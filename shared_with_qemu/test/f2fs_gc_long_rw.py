#!/usr/bin/env python3
"""Long-running F2FS GC pressure runner backed by the rw_test provider."""

from __future__ import annotations

import argparse
import errno
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rw_test import (
    DEFAULT_BLOCK,
    DEFAULT_DUMP_MAX,
    MMAP_FILE_SZ,
    PAGE,
    PatternConfig,
    RA_SPAN,
    TARGET_LARGE,
    TargetSpec,
    build_builtin_matrix_cases,
    do_write_region,
    drop_caches,
    fill_largefolio_pattern,
    filter_matrix_cases,
    generate_expected_direct,
    make_case,
    parse_bytes,
    prepare_baseline,
    read_fd_discard,
    run_matrix_case,
    verify_stream,
)


DEFAULT_PLAIN_IMG_MB = 96
DEFAULT_INLINE_IMG_MB = 96
DEFAULT_GARBAGE_FILE_MB = 4
DEFAULT_RESERVE_FREE_MB = 12
DEFAULT_CHURN_HEADROOM_FILES = 2
DEFAULT_MOUNT_OPTS = "background_gc=sync,discard"
DEFAULT_INLINE_MOUNT_OPTS = "inlinecrypt,background_gc=sync,discard"
DEFAULT_LARGEFOLIO_FILE_MB = MMAP_FILE_SZ // (1024 * 1024)
DEFAULT_LARGEFOLIO_SCAN_MB = RA_SPAN // (1024 * 1024)
DEFAULT_LARGEFOLIO_PATCH_PAGES = 2
DEFAULT_LARGEFOLIO_PATCH_SIZE = 0
DEFAULT_LARGEFOLIO_HOTSET_SIZE = 0
DEFAULT_LARGEFOLIO_VERIFY_EVERY = 1
DEFAULT_LARGEFOLIO_FSYNC_EVERY = 1
DEFAULT_DISK_VERIFY_EVERY = 1
DEFAULT_FIXED_BASELINE_LEN = 64 * 1024
DEFAULT_FIXED_WRITE_OFFSET = 0
DEFAULT_FIXED_WRITE_SIZE = 65535
DEFAULT_ALT_HALF_DELAY_MS = 50
DEFAULT_ALT_HALF_READ_PASSES = 4
LARGEFOLIO_READ_CHUNK = 256 * 1024
CHURN_CHUNK = 1024 * 1024
CHURN_PATTERN = bytes((0x41 + (i % 23)) & 0xFF for i in range(CHURN_CHUNK))


@dataclass
class SharedState:
    stop_event: threading.Event = field(default_factory=threading.Event)
    error_lock: threading.Lock = field(default_factory=threading.Lock)
    first_error: Optional[str] = None

    def fail(self, msg: str) -> None:
        with self.error_lock:
            if self.first_error is None:
                self.first_error = msg
                self.stop_event.set()

    def raise_if_failed(self) -> None:
        with self.error_lock:
            msg = self.first_error
        if msg is not None:
            raise RuntimeError(msg)


@dataclass
class MountedTarget:
    label: str
    mount_root: str
    target_dir: str
    device: Optional[str]
    image_path: Optional[str] = None
    loopdev: Optional[str] = None
    auto_cleanup: bool = False

    def to_target_spec(self) -> TargetSpec:
        return TargetSpec(label=self.label, directory=self.target_dir)


def run_cmd(
    argv: list[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"[cmd] {shlex.join(argv)}", flush=True)
    cp = subprocess.run(argv, capture_output=capture_output, text=text, check=False)
    if check and cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        stdout = (cp.stdout or "").strip()
        detail = stderr or stdout or f"exit={cp.returncode}"
        raise RuntimeError(f"command failed: {shlex.join(argv)} :: {detail}")
    return cp


def require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("run as root: mkfs/mount/losetup/drop_caches require it")


def need_cmds(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise SystemExit(f"missing command(s): {', '.join(missing)}")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def recreate_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def write_sparse_file(path: str, size_bytes: int) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size_bytes)
    finally:
        os.close(fd)


def attach_loop(image_path: str) -> str:
    cp = run_cmd(["losetup", "--find", "--show", image_path], capture_output=True)
    return cp.stdout.strip()


def mount_f2fs(
    device: str,
    mount_root: str,
    label: str,
    mount_opts: str,
    *,
    mkfs_features: Optional[list[str]] = None,
) -> None:
    ensure_dir(mount_root)
    argv = ["mkfs.f2fs", "-f", "-l", label]
    for feature in mkfs_features or []:
        argv.extend(["-O", feature])
    argv.extend(["-s", "1", device])
    run_cmd(argv)
    run_cmd(["mount", "-t", "f2fs", "-o", mount_opts, device, mount_root])


def findmnt_value(field: str, target: str) -> str:
    cp = run_cmd(
        ["findmnt", "-no", field, "--target", target],
        capture_output=True,
    )
    value = cp.stdout.strip()
    if not value:
        raise RuntimeError(f"findmnt returned empty {field} for {target}")
    return value


def verify_f2fs_path(path: str) -> tuple[str, Optional[str]]:
    mount_root = findmnt_value("TARGET", path)
    fs_type = findmnt_value("FSTYPE", path)
    if fs_type != "f2fs":
        raise RuntimeError(f"{path} is on {fs_type}, expected f2fs")
    source = findmnt_value("SOURCE", path)
    device = source if source.startswith("/dev/") else None
    return mount_root, device


def ensure_mount_has_option(path: str, option: str) -> None:
    opts = findmnt_value("OPTIONS", path)
    if f",{option}," not in f",{opts},":
        raise RuntimeError(f"{path} is not mounted with {option}; current options: {opts}")


def fscrypt_status_text(path: str) -> str:
    cp = run_cmd(["fscrypt", "status", path], capture_output=True, check=False)
    return ((cp.stdout or "") + (cp.stderr or "")).strip()


def ensure_inline_fscrypt_dir(
    mount_root: str,
    enc_dir_name: str,
    key_path: str,
    protector_name: str,
) -> str:
    ensure_mount_has_option(mount_root, "inlinecrypt")

    run_cmd(["fscrypt", "setup", mount_root, "--quiet"], check=False)

    enc_dir = os.path.join(mount_root, enc_dir_name)
    ensure_dir(enc_dir)

    status = fscrypt_status_text(enc_dir)
    if "encrypted with fscrypt" not in status:
        if os.listdir(enc_dir):
            raise RuntimeError(f"inline enc dir exists but is plain/non-empty: {enc_dir}")
        run_cmd(
            [
                "fscrypt",
                "encrypt",
                enc_dir,
                "--source=raw_key",
                "--name",
                protector_name,
                "--key",
                key_path,
                "--quiet",
            ]
        )
        status = fscrypt_status_text(enc_dir)

    if "encrypted with fscrypt" not in status:
        raise RuntimeError(f"failed to encrypt inline dir: {enc_dir}\n{status}")

    run_cmd(["fscrypt", "unlock", enc_dir, "--key", key_path], check=False)

    cp = run_cmd(["lsattr", "-d", enc_dir], capture_output=True, check=False)
    attr = (cp.stdout or "").strip().split()
    if not attr or "E" not in attr[0]:
        raise RuntimeError(f"inline enc dir missing fscrypt E attribute: {enc_dir}")

    return enc_dir


def prepare_target_dir(root: str, label: str) -> str:
    path = os.path.join(root, f"gc_long_{label}")
    recreate_dir(path)
    return path


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def write_garbage_file(path: str, size_bytes: int) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        remaining = size_bytes
        while remaining > 0:
            chunk = CHURN_PATTERN if remaining >= len(CHURN_PATTERN) else CHURN_PATTERN[:remaining]
            written = os.write(fd, chunk)
            if written <= 0:
                raise OSError(errno.EIO, f"short write to {path}")
            remaining -= written
        os.fsync(fd)
    finally:
        os.close(fd)


class ChurnThread(threading.Thread):
    def __init__(
        self,
        target: MountedTarget,
        state: SharedState,
        *,
        garbage_file_bytes: int,
        reserve_free_bytes: int,
        headroom_files: int,
        continuous: bool,
    ) -> None:
        super().__init__(name=f"churn-{target.label}", daemon=True)
        self.target = target
        self.state = state
        self.garbage_file_bytes = garbage_file_bytes
        self.reserve_free_bytes = reserve_free_bytes
        self.headroom_files = max(0, headroom_files)
        self.high_watermark = reserve_free_bytes + self.headroom_files * garbage_file_bytes
        self.churn_dir = os.path.join(target.mount_root, f".gc_long_churn_{target.label}")
        self.live_files: deque[str] = deque()
        self.seq = 0
        self.continuous = continuous

    def _delete_oldest(self) -> bool:
        if not self.live_files:
            return False
        path = self.live_files.popleft()
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True

    def _trim_until_safe(self) -> None:
        while free_bytes(self.target.mount_root) < self.reserve_free_bytes and self.live_files:
            self._delete_oldest()

    def wait_for_pressure(self, timeout_sec: int) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            self.state.raise_if_failed()
            if free_bytes(self.target.mount_root) <= self.high_watermark:
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"{self.target.label}: failed to reach low-free-space pressure within {timeout_sec}s"
        )

    def run(self) -> None:
        try:
            recreate_dir(self.churn_dir)
            while not self.state.stop_event.is_set():
                current_free = free_bytes(self.target.mount_root)
                if self.continuous and current_free < self.reserve_free_bytes:
                    if not self._delete_oldest():
                        time.sleep(0.05)
                    continue
                if current_free <= self.high_watermark:
                    if self.continuous:
                        time.sleep(0.05)
                        continue
                    return

                path = os.path.join(self.churn_dir, f"churn_{self.seq:06d}.bin")
                self.seq += 1
                try:
                    write_garbage_file(path, self.garbage_file_bytes)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        if self.continuous:
                            self._trim_until_safe()
                            continue
                        return
                    raise
                self.live_files.append(path)
                if self.continuous:
                    self._trim_until_safe()
        except Exception as exc:
            self.state.fail(f"{self.name}: {exc}")


class GcThread(threading.Thread):
    def __init__(
        self,
        target: MountedTarget,
        state: SharedState,
        *,
        gc_window_sec: int,
        poll_interval_sec: float,
    ) -> None:
        super().__init__(name=f"gc-{target.label}", daemon=True)
        self.target = target
        self.state = state
        self.gc_window_sec = gc_window_sec
        self.poll_interval_sec = poll_interval_sec

    def _run_gc_once(self) -> None:
        if self.target.device is not None:
            dev_name = os.path.basename(os.path.realpath(self.target.device))
            cp = run_cmd(
                ["f2fs_io", "gc_urgent", dev_name, "run", str(self.gc_window_sec)],
                capture_output=True,
                check=False,
            )
            if cp.returncode == 0:
                return
        run_cmd(["f2fs_io", "gc", "1", self.target.mount_root], check=False)

    def run(self) -> None:
        try:
            while not self.state.stop_event.is_set():
                self._run_gc_once()
                self.state.stop_event.wait(self.poll_interval_sec)
        except Exception as exc:
            self.state.fail(f"{self.name}: {exc}")


def build_largefolio_seed(size_bytes: int, scan_bytes: int) -> bytearray:
    size_bytes = max(0, size_bytes)
    scan_bytes = max(0, min(scan_bytes, size_bytes))
    expected = bytearray(size_bytes)
    chunk = bytes((i ^ 0x5A) & 0xFF for i in range(TARGET_LARGE))

    pos = 0
    while pos < scan_bytes:
        take = min(len(chunk), scan_bytes - pos)
        expected[pos:pos + take] = chunk[:take]
        pos += take
    return expected


def build_largefolio_patch(seq: int, length: int) -> bytes:
    base = (0x30 + seq * 17) & 0xFF
    return bytes(((base + i) & 0xFF) for i in range(length))


def round_up(value: int, align: int) -> int:
    if align <= 0:
        raise ValueError("align must be > 0")
    return ((value + align - 1) // align) * align


class LargeFolioThread(threading.Thread):
    def __init__(
        self,
        target: MountedTarget,
        state: SharedState,
        *,
        file_size_bytes: int,
        scan_bytes: int,
        read_passes: int,
        patch_pages: int,
        patch_size_bytes: int,
        hotset_bytes: int,
        fsync_every: int,
        verify_every: int,
    ) -> None:
        super().__init__(name=f"largefolio-{target.label}", daemon=True)
        self.target = target
        self.state = state
        self.file_size_bytes = max(TARGET_LARGE, file_size_bytes)
        self.scan_bytes = max(TARGET_LARGE, min(scan_bytes, self.file_size_bytes))
        self.read_passes = max(1, read_passes)
        self.patch_pages = max(1, patch_pages)
        requested_patch = patch_size_bytes if patch_size_bytes > 0 else self.patch_pages * PAGE
        self.patch_len = min(round_up(max(PAGE, requested_patch), PAGE), self.scan_bytes)
        self.patch_stride = max(TARGET_LARGE, self.patch_len)
        if hotset_bytes > 0:
            self.hotset_bytes = min(self.scan_bytes, round_up(max(self.patch_len, hotset_bytes), PAGE))
        else:
            self.hotset_bytes = self.scan_bytes
        self.fsync_every = max(1, fsync_every)
        self.verify_every = max(1, verify_every)
        self.file_path = os.path.join(target.target_dir, f"{target.label}_largefolio_gc.bin")
        self.window_count = max(1, (self.hotset_bytes + self.patch_stride - 1) // self.patch_stride)
        self.subpages_per_window = max(1, TARGET_LARGE // PAGE)
        self.expected = build_largefolio_seed(self.file_size_bytes, self.scan_bytes)
        self.prepared = False

    def _prepare_file(self) -> None:
        ensure_dir(os.path.dirname(self.file_path) or ".")
        fd = os.open(self.file_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            fill_largefolio_pattern(fd, self.file_size_bytes, self.scan_bytes)
        finally:
            os.close(fd)
        self.prepared = True

    def prepare_initial(self) -> None:
        if not self.prepared:
            self._prepare_file()
        st = os.stat(self.file_path)
        print(
            f"[LF-SETUP] label={self.target.label} path={self.file_path} "
            f"ino={st.st_ino} size={st.st_size} scan={self.scan_bytes} "
            f"hotset={self.hotset_bytes} patch_len={self.patch_len}",
            flush=True,
        )

    def _next_patch(self, seq: int) -> tuple[int, bytes]:
        window = seq % self.window_count
        base = window * self.patch_stride

        if seq % 8 == 7 and self.patch_len >= 2 * PAGE:
            offset = base + max(0, min(TARGET_LARGE, self.patch_stride) - PAGE)
        else:
            subpage = (seq * 7) % self.subpages_per_window
            offset = base + subpage * PAGE

        max_offset = max(0, self.scan_bytes - self.patch_len)
        offset = min(offset, max_offset)
        patch = build_largefolio_patch(seq, min(self.patch_len, self.scan_bytes - offset))
        return offset, patch

    def _verify_disk(self, seq: int) -> None:
        os.sync()
        drop_caches(3)
        fd = os.open(self.file_path, os.O_RDONLY)
        try:
            ok = verify_stream(
                fd,
                0,
                len(self.expected),
                bytes(self.expected),
                None,
                LARGEFOLIO_READ_CHUNK,
                "direct",
            )
        finally:
            os.close(fd)

        if not ok:
            raise RuntimeError(
                f"{self.target.label}: largefolio disk verify mismatch after round={seq}"
            )

    def run(self) -> None:
        try:
            if not self.prepared:
                self._prepare_file()
            seq = 0
            while not self.state.stop_event.is_set():
                fd = os.open(self.file_path, os.O_RDWR)
                try:
                    scanned = read_fd_discard(
                        fd,
                        0,
                        self.scan_bytes,
                        LARGEFOLIO_READ_CHUNK,
                        passes=self.read_passes,
                    )
                    offset, patch = self._next_patch(seq)
                    written = os.pwrite(fd, patch, offset)
                    if written != len(patch):
                        raise OSError(errno.EIO, f"short pwrite: wrote {written} expected {len(patch)}")
                    need_sync = ((seq + 1) % self.fsync_every) == 0
                    need_verify = ((seq + 1) % self.verify_every) == 0
                    if need_sync or need_verify:
                        os.fsync(fd)
                finally:
                    os.close(fd)

                self.expected[offset:offset + len(patch)] = patch
                print(
                    f"[LF] label={self.target.label} round={seq} scan={scanned} "
                    f"patch_off={offset} patch_len={len(patch)} "
                    f"fsync={'y' if need_sync else 'n'} verify={'y' if need_verify else 'n'}",
                    flush=True,
                )

                if need_verify:
                    self._verify_disk(seq)

                seq += 1
        except Exception as exc:
            self.state.fail(f"{self.name}: {exc}")


def build_baseline_expected(baseline_kind: str, baseline_len: int) -> bytearray:
    if baseline_kind == "existing_a":
        return bytearray(b"A" * baseline_len)
    if baseline_kind == "hole":
        return bytearray(b"\x00" * baseline_len)
    raise ValueError(f"unknown baseline kind: {baseline_kind}")


def verify_expected_file(
    path: str,
    expected: bytes,
    chunk_size: int,
    read_mode: str,
    *,
    cold_read: bool,
) -> bool:
    if cold_read:
        print("[verify] alt-half disk-mode: os.sync + drop_caches(3) + reopen", flush=True)
        try:
            os.sync()
            drop_caches(3)
        except PermissionError:
            print("[FAIL] drop_caches requires root (permission denied).", flush=True)
            return False
        except Exception as exc:
            print(f"[FAIL] drop_caches error: {exc}", flush=True)
            return False

    st = os.stat(path)
    if st.st_size != len(expected):
        print(f"[FAIL] alt-half size mismatch: actual={st.st_size} expected={len(expected)}", flush=True)
        return False

    fd = os.open(path, os.O_RDONLY)
    try:
        return verify_stream(fd, 0, len(expected), expected, None, chunk_size, read_mode)
    finally:
        os.close(fd)


def warm_file_span(path: str, offset: int, size: int, chunk_size: int, passes: int) -> int:
    if size <= 0 or passes <= 0:
        return 0

    fd = os.open(path, os.O_RDONLY)
    try:
        return read_fd_discard(fd, offset, size, chunk_size, passes=passes)
    finally:
        os.close(fd)


@dataclass
class AlternatingHalfRunner:
    target: TargetSpec
    path: str
    baseline_kind: str
    baseline_len: int
    half_len: int
    config: PatternConfig
    read_passes: int
    delay_sec: float
    expected: bytearray
    next_half: int = 0
    steps: int = 0

    @classmethod
    def prepare(
        cls,
        target: MountedTarget,
        *,
        baseline_kind: str,
        baseline_len: int,
        chunk_size: int,
        drop_after_prepare: bool,
        config: PatternConfig,
        read_passes: int,
        delay_sec: float,
    ) -> "AlternatingHalfRunner":
        if baseline_len <= 0 or baseline_len % 2 != 0:
            raise RuntimeError(f"{target.label}: alt-half baseline_len must be a positive even number")

        half_len = baseline_len // 2
        if half_len < TARGET_LARGE:
            raise RuntimeError(
                f"{target.label}: alt-half half_len={half_len} is too small for large-folio targeting"
            )

        path = os.path.join(target.target_dir, f"{target.label}_alt_half_gc.bin")
        if not prepare_baseline(
            path,
            baseline_kind,
            baseline_len,
            chunk_size=chunk_size,
            drop_after_prepare=drop_after_prepare,
        ):
            raise RuntimeError(f"{target.label}: failed to prepare alt-half file")

        st = os.stat(path)
        print(
            f"[ALT-SETUP] label={target.label} path={path} ino={st.st_ino} "
            f"baseline_kind={baseline_kind} baseline_len={baseline_len} half_len={half_len} "
            f"read_passes={read_passes} delay_ms={int(delay_sec * 1000)}",
            flush=True,
        )

        return cls(
            target=target.to_target_spec(),
            path=path,
            baseline_kind=baseline_kind,
            baseline_len=baseline_len,
            half_len=half_len,
            config=config,
            read_passes=max(1, read_passes),
            delay_sec=max(0.0, delay_sec),
            expected=build_baseline_expected(baseline_kind, baseline_len),
        )

    def run_step(self, *, verify_mode: str, fsync_after: bool, chunk_size: int) -> None:
        active_half = self.next_half
        passive_half = 1 - active_half
        active_off = active_half * self.half_len
        passive_off = passive_half * self.half_len
        active_name = "front" if active_half == 0 else "back"
        passive_name = "back" if active_half == 0 else "front"

        if not do_write_region(
            self.path,
            active_off,
            self.half_len,
            self.config,
            fsync_after=fsync_after,
            dump=False,
            block_size=DEFAULT_BLOCK,
            dump_max=DEFAULT_DUMP_MAX,
        ):
            raise RuntimeError(
                f"alt-half write failure: target={self.target.label} half={active_name} step={self.steps}"
            )

        self.expected[active_off:active_off + self.half_len] = generate_expected_direct(
            active_off,
            self.half_len,
            self.config,
        )

        warmed = warm_file_span(
            self.path,
            passive_off,
            self.half_len,
            chunk_size,
            self.read_passes,
        )

        if not verify_expected_file(
            self.path,
            bytes(self.expected),
            chunk_size,
            self.config.readback,
            cold_read=(verify_mode == "disk"),
        ):
            raise RuntimeError(
                f"alt-half verify mismatch: target={self.target.label} step={self.steps} verify_mode={verify_mode}"
            )

        print(
            f"[ALT] label={self.target.label} step={self.steps} wrote={active_name}@{active_off}+{self.half_len} "
            f"warmed={passive_name}@{passive_off}+{self.half_len} warm_total={warmed} "
            f"verify_mode={verify_mode} fsync={'y' if fsync_after else 'n'}",
            flush=True,
        )

        self.steps += 1
        self.next_half = passive_half

        if self.delay_sec > 0:
            time.sleep(self.delay_sec)


def setup_auto_image(
    *,
    label: str,
    work_dir: str,
    mount_root: str,
    img_mb: int,
    mount_opts: str,
    mkfs_features: Optional[list[str]] = None,
) -> MountedTarget:
    image_path = os.path.join(work_dir, f"{label}.img")
    write_sparse_file(image_path, img_mb * 1024 * 1024)
    loopdev = attach_loop(image_path)
    mount_f2fs(loopdev, mount_root, f"gc_long_{label}", mount_opts, mkfs_features=mkfs_features)
    target_dir = prepare_target_dir(mount_root, label)
    return MountedTarget(
        label=label,
        mount_root=mount_root,
        target_dir=target_dir,
        device=loopdev,
        image_path=image_path,
        loopdev=loopdev,
        auto_cleanup=True,
    )


def setup_block_device(
    *,
    label: str,
    device: str,
    mount_root: str,
    mount_opts: str,
    mkfs_features: Optional[list[str]] = None,
) -> MountedTarget:
    mount_f2fs(device, mount_root, f"gc_long_{label}", mount_opts, mkfs_features=mkfs_features)
    target_dir = prepare_target_dir(mount_root, label)
    return MountedTarget(
        label=label,
        mount_root=mount_root,
        target_dir=target_dir,
        device=device,
        auto_cleanup=True,
    )


def setup_existing_root(*, label: str, root: str) -> MountedTarget:
    mount_root, device = verify_f2fs_path(root)
    target_dir = prepare_target_dir(root, label)
    return MountedTarget(
        label=label,
        mount_root=mount_root,
        target_dir=target_dir,
        device=device,
    )


def setup_auto_inline_image(
    *,
    label: str,
    work_dir: str,
    mount_root: str,
    img_mb: int,
    mount_opts: str,
    enc_dir_name: str,
    key_path: str,
) -> MountedTarget:
    mounted = setup_auto_image(
        label=label,
        work_dir=work_dir,
        mount_root=mount_root,
        img_mb=img_mb,
        mount_opts=mount_opts,
        mkfs_features=["encrypt"],
    )
    enc_root = ensure_inline_fscrypt_dir(mounted.mount_root, enc_dir_name, key_path, f"gc-long-{label}")
    mounted.target_dir = prepare_target_dir(enc_root, label)
    return mounted


def setup_existing_inline_root(
    *,
    label: str,
    root: str,
    enc_dir_name: str,
    key_path: str,
) -> MountedTarget:
    mount_root, device = verify_f2fs_path(root)
    enc_root = ensure_inline_fscrypt_dir(mount_root, enc_dir_name, key_path, f"gc-long-{label}")
    target_dir = prepare_target_dir(enc_root, label)
    return MountedTarget(
        label=label,
        mount_root=mount_root,
        target_dir=target_dir,
        device=device,
    )


def setup_inline_block_device(
    *,
    label: str,
    device: str,
    mount_root: str,
    mount_opts: str,
    enc_dir_name: str,
    key_path: str,
) -> MountedTarget:
    mounted = setup_block_device(
        label=label,
        device=device,
        mount_root=mount_root,
        mount_opts=mount_opts,
        mkfs_features=["encrypt"],
    )
    enc_root = ensure_inline_fscrypt_dir(mounted.mount_root, enc_dir_name, key_path, f"gc-long-{label}")
    mounted.target_dir = prepare_target_dir(enc_root, label)
    return mounted


def cleanup_target(target: MountedTarget) -> None:
    if target.auto_cleanup and os.path.isdir(target.mount_root):
        run_cmd(["umount", target.mount_root], check=False)
    if target.loopdev is not None:
        run_cmd(["losetup", "-d", target.loopdev], check=False)


def build_config(args: argparse.Namespace) -> PatternConfig:
    return PatternConfig(
        mode=args.pattern_mode,
        token=args.token.encode("utf-8"),
        seed=args.seed,
        chunk_size=args.chunk,
        pattern_gen=args.pattern_gen,
        readback=args.readback,
    )


def build_case_list(args: argparse.Namespace):
    if args.target_file_scheme == "alt-half":
        return []

    if args.target_file_scheme == "one":
        return [
            make_case(
                "fixed",
                args.fixed_baseline_kind,
                args.fixed_baseline_len,
                "overwrite",
                args.fixed_write_offset,
                args.fixed_write_size,
                read_before_write=True,
            )
        ]

    if args.target_file_scheme == "two":
        return [
            make_case(
                "fixed",
                args.fixed_baseline_kind,
                args.fixed_baseline_len,
                "overwrite",
                args.fixed_write_offset,
                args.fixed_write_size,
            ),
            make_case(
                "fixed",
                args.fixed_baseline_kind,
                args.fixed_baseline_len,
                "overwrite",
                args.fixed_write_offset,
                args.fixed_write_size,
                read_before_write=True,
            ),
        ]

    return filter_matrix_cases(
        build_builtin_matrix_cases(include_read_then_write=args.include_read_then_write),
        args.baseline_kind,
        args.write_style,
        args.case_filter,
        args.include_read_then_write,
    )


def join_thread(thread: threading.Thread, timeout: float = 5.0) -> None:
    thread.join(timeout=timeout)


def precreate_case_files(
    mounted: list[MountedTarget],
    cases,
    *,
    chunk_size: int,
    drop_after_prepare: bool,
) -> None:
    for target in mounted:
        for case in cases:
            path = os.path.join(target.target_dir, case.file_name(target.label))
            if not prepare_baseline(
                path,
                case.baseline_kind,
                case.baseline_len,
                chunk_size=chunk_size,
                drop_after_prepare=drop_after_prepare,
            ):
                raise RuntimeError(
                    f"failed to precreate fixed case file: target={target.label} case={case.name}"
                )
            st = os.stat(path)
            print(
                f"[PRECREATE] label={target.label} path={path} ino={st.st_ino} size={st.st_size}",
                flush=True,
            )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Run long-lived F2FS GC pressure while reusing rw_test matrix verification. "
            "Plain uses an auto-created small loop image. Inline can be auto-provisioned "
            "as an inlinecrypt mount with an fscrypt-encrypted directory, or provided "
            "through an existing/root block device path."
        )
    )
    ap.add_argument("--work-dir", default="/tmp/f2fs_gc_long_rw", help="runner scratch directory")
    ap.add_argument("--plain-img-mb", type=int, default=DEFAULT_PLAIN_IMG_MB, help="plain loop image size in MiB")
    ap.add_argument(
        "--plain-mount",
        default="/mnt/f2fs_gc_long_plain",
        help="mount point for the auto-provisioned plain image",
    )
    ap.add_argument(
        "--inline-root",
        help="already-mounted f2fs root for the inlinecrypt target",
    )
    ap.add_argument(
        "--inline-device",
        help="block device for the inlinecrypt target; the script will mkfs+mount it with encrypt support",
    )
    ap.add_argument(
        "--inline-img-mb",
        type=int,
        default=0,
        help="auto-provision an inlinecrypt loop image of this size in MiB",
    )
    ap.add_argument(
        "--inline-mount",
        default="/mnt/f2fs_gc_long_inline",
        help="mount point used with inline target provisioning",
    )
    ap.add_argument(
        "--inline-mount-opts",
        default=DEFAULT_INLINE_MOUNT_OPTS,
        help="mount options for auto-mounted inline targets",
    )
    ap.add_argument(
        "--inline-enc-dir",
        default="enc_test",
        help="fscrypt encrypted directory name created under the inline mount root",
    )
    ap.add_argument(
        "--fscrypt-key",
        default="/opt/test-secrets/fscrypt-ci.key",
        help="raw key file used to create/unlock the inline fscrypt directory",
    )
    ap.add_argument(
        "--inline-only",
        action="store_true",
        help="skip the plain target and exercise only the inline target first",
    )
    ap.add_argument(
        "--target-order",
        choices=["inline-first", "plain-first"],
        default="inline-first",
        help="target iteration order when both inline and plain are present",
    )
    ap.add_argument(
        "--allow-plain-only",
        action="store_true",
        help="allow the run to proceed without an inline target",
    )
    ap.add_argument(
        "--mount-opts",
        default=DEFAULT_MOUNT_OPTS,
        help="mount options for auto-mounted F2FS targets",
    )
    ap.add_argument("--runtime-sec", type=int, default=90, help="how long to keep running matrix cases")
    ap.add_argument(
        "--startup-pressure-timeout",
        type=int,
        default=20,
        help="seconds to wait for low-free-space pressure before starting checks",
    )
    ap.add_argument(
        "--garbage-file-size",
        default=f"{DEFAULT_GARBAGE_FILE_MB}m",
        help="size of each churn file",
    )
    ap.add_argument(
        "--reserve-free",
        default=f"{DEFAULT_RESERVE_FREE_MB}m",
        help="keep free space around this watermark to make GC selection likely",
    )
    ap.add_argument(
        "--churn-headroom-files",
        type=int,
        default=DEFAULT_CHURN_HEADROOM_FILES,
        help="keep at most this many extra garbage files beyond reserve-free before churn pauses",
    )
    ap.add_argument(
        "--pressure-mode",
        choices=["continuous", "prefill"],
        default="continuous",
        help="continuous keeps creating/deleting garbage files; prefill only fills once to low free space",
    )
    ap.add_argument("--gc-window-sec", type=int, default=2, help="duration of each gc_urgent window")
    ap.add_argument("--gc-poll-sec", type=float, default=0.5, help="pause between GC trigger attempts")
    ap.add_argument(
        "--target-file-scheme",
        choices=["matrix", "one", "two", "alt-half"],
        default="matrix",
        help="matrix keeps the builtin case set; one/two exercise fixed files; alt-half alternates writes between the two halves of one fixed file",
    )
    ap.add_argument(
        "--fixed-baseline-kind",
        choices=["existing_a", "hole"],
        default="hole",
        help="baseline kind used by the one/two fixed-file schemes",
    )
    ap.add_argument(
        "--fixed-baseline-len",
        default=str(DEFAULT_FIXED_BASELINE_LEN),
        help="baseline length used by the one/two fixed-file schemes",
    )
    ap.add_argument(
        "--fixed-write-offset",
        default=str(DEFAULT_FIXED_WRITE_OFFSET),
        help="overwrite offset used by the one/two fixed-file schemes",
    )
    ap.add_argument(
        "--fixed-write-size",
        default=str(DEFAULT_FIXED_WRITE_SIZE),
        help="overwrite size used by the one/two fixed-file schemes",
    )
    ap.add_argument(
        "--alt-half-delay-ms",
        type=int,
        default=DEFAULT_ALT_HALF_DELAY_MS,
        help="sleep after warming the opposite half before the next alt-half overwrite",
    )
    ap.add_argument(
        "--alt-half-read-passes",
        type=int,
        default=DEFAULT_ALT_HALF_READ_PASSES,
        help="read passes used to keep the opposite half hot in alt-half mode",
    )
    ap.add_argument(
        "--largefolio-worker",
        action="store_true",
        help="add a large-file buffered I/O worker that repeatedly ramps readahead and verifies disk data",
    )
    ap.add_argument(
        "--largefolio-file-size",
        default=f"{DEFAULT_LARGEFOLIO_FILE_MB}m",
        help="largefolio worker file size",
    )
    ap.add_argument(
        "--largefolio-scan-size",
        default=f"{DEFAULT_LARGEFOLIO_SCAN_MB}m",
        help="bytes per round to read sequentially and keep hot for large folio pressure",
    )
    ap.add_argument(
        "--largefolio-read-passes",
        type=int,
        default=2,
        help="sequential read passes per largefolio round",
    )
    ap.add_argument(
        "--largefolio-patch-pages",
        type=int,
        default=DEFAULT_LARGEFOLIO_PATCH_PAGES,
        help="pages to overwrite per largefolio round",
    )
    ap.add_argument(
        "--largefolio-patch-size",
        default=str(DEFAULT_LARGEFOLIO_PATCH_SIZE),
        help="bytes to overwrite per largefolio round; overrides --largefolio-patch-pages when > 0",
    )
    ap.add_argument(
        "--largefolio-hotset-size",
        default=str(DEFAULT_LARGEFOLIO_HOTSET_SIZE),
        help="bytes of the scan region to keep rewriting repeatedly; 0 means the whole scan region",
    )
    ap.add_argument(
        "--largefolio-fsync-every",
        type=int,
        default=DEFAULT_LARGEFOLIO_FSYNC_EVERY,
        help="fsync the largefolio worker every N rounds; larger values keep more dirty page cache in flight",
    )
    ap.add_argument(
        "--largefolio-verify-every",
        type=int,
        default=DEFAULT_LARGEFOLIO_VERIFY_EVERY,
        help="verify on-disk contents every N largefolio rounds",
    )
    ap.add_argument(
        "--include-read-then-write",
        action="store_true",
        default=True,
        help="include read-then-write variants [default on]",
    )
    ap.add_argument(
        "--no-read-then-write",
        action="store_false",
        dest="include_read_then_write",
        help="disable read-then-write variants",
    )
    ap.add_argument(
        "--baseline-kind",
        action="append",
        choices=["existing_a", "hole"],
        default=[],
        help="filter builtin cases by baseline kind",
    )
    ap.add_argument(
        "--write-style",
        action="append",
        choices=["overwrite", "append"],
        default=[],
        help="filter builtin cases by write style",
    )
    ap.add_argument(
        "--case-filter",
        action="append",
        default=[],
        help="substring filter for builtin case names",
    )
    ap.add_argument(
        "--read-before-write-passes",
        type=int,
        default=1,
        help="number of warmup read passes for read-then-write cases",
    )
    ap.add_argument(
        "--verify-mode",
        choices=["cache", "disk"],
        default="disk",
        help="use disk mode to force immediate mismatch detection",
    )
    ap.add_argument(
        "--disk-verify-every",
        type=int,
        default=DEFAULT_DISK_VERIFY_EVERY,
        help="when verify-mode=disk, run full disk verification every N matrix cases and use cache verification for the others",
    )
    ap.add_argument(
        "--drop-after-prepare",
        action="store_true",
        default=True,
        help="drop caches after baseline prepare [default on]",
    )
    ap.add_argument(
        "--no-drop-after-prepare",
        action="store_false",
        dest="drop_after_prepare",
        help="disable drop_caches after baseline prepare",
    )
    ap.add_argument(
        "--fsync",
        action="store_true",
        default=True,
        help="fsync after the structured write [default on]",
    )
    ap.add_argument(
        "--no-fsync",
        action="store_false",
        dest="fsync",
        help="skip fsync after the structured write",
    )
    ap.add_argument("--pattern-mode", choices=["filepos", "repeat", "counter"], default="filepos")
    ap.add_argument("--token", default="PyWrtDta", help="pattern token")
    ap.add_argument("--seed", type=int, default=0, help="pattern seed for counter mode")
    ap.add_argument("--chunk", default="1m", help="chunk size used by the rw_test provider")
    ap.add_argument(
        "--pattern-gen",
        choices=["stream", "direct"],
        default="stream",
        help="pattern generation mode",
    )
    ap.add_argument(
        "--readback",
        choices=["stream", "direct"],
        default="stream",
        help="readback comparison mode",
    )
    return ap


def parse_args() -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args()
    inline_inputs = sum(bool(item) for item in (args.inline_root, args.inline_device, args.inline_img_mb))
    if inline_inputs > 1:
        parser.error("--inline-root, --inline-device, and --inline-img-mb are mutually exclusive")
    args.chunk = parse_bytes(args.chunk)
    args.garbage_file_size = parse_bytes(args.garbage_file_size)
    args.reserve_free = parse_bytes(args.reserve_free)
    args.fixed_baseline_len = parse_bytes(args.fixed_baseline_len)
    args.fixed_write_offset = parse_bytes(args.fixed_write_offset)
    args.fixed_write_size = parse_bytes(args.fixed_write_size)
    args.largefolio_file_size = parse_bytes(args.largefolio_file_size)
    args.largefolio_scan_size = parse_bytes(args.largefolio_scan_size)
    args.largefolio_patch_size = parse_bytes(args.largefolio_patch_size)
    args.largefolio_hotset_size = parse_bytes(args.largefolio_hotset_size)
    return args


def main() -> int:
    args = parse_args()
    require_root()
    need_cmds("findmnt", "f2fs_io", "losetup", "mkfs.f2fs", "mount", "umount")
    if args.inline_root or args.inline_device or args.inline_img_mb:
        need_cmds("fscrypt", "lsattr")

    cases = build_case_list(args)
    if not cases and args.target_file_scheme != "alt-half":
        print("[FAIL] no cases selected", file=sys.stderr)
        return 2

    inline_requested = bool(args.inline_root or args.inline_device or args.inline_img_mb)
    if args.inline_only and not inline_requested:
        print("[FAIL] --inline-only requires an inline target", file=sys.stderr)
        return 2

    if not args.allow_plain_only and not inline_requested:
        print(
            "[FAIL] inline coverage is required: pass --inline-root, --inline-device, or --inline-img-mb, "
            "or use --allow-plain-only for a plain-only smoke run",
            file=sys.stderr,
        )
        return 2

    recreate_dir(args.work_dir)
    state = SharedState()
    config = build_config(args)
    mounted: list[MountedTarget] = []
    churn_threads: list[ChurnThread] = []
    gc_threads: list[GcThread] = []
    largefolio_threads: list[LargeFolioThread] = []
    pending_largefolio: list[LargeFolioThread] = []
    pending_churn: list[ChurnThread] = []
    pending_gc: list[GcThread] = []
    alt_half_runners: list[AlternatingHalfRunner] = []

    try:
        if not args.inline_only:
            plain = setup_auto_image(
                label="plain",
                work_dir=args.work_dir,
                mount_root=args.plain_mount,
                img_mb=args.plain_img_mb,
                mount_opts=args.mount_opts,
            )
            mounted.append(plain)

        if args.inline_img_mb:
            mounted.append(
                setup_auto_inline_image(
                    label="inline",
                    work_dir=args.work_dir,
                    mount_root=args.inline_mount,
                    img_mb=args.inline_img_mb,
                    mount_opts=args.inline_mount_opts,
                    enc_dir_name=args.inline_enc_dir,
                    key_path=args.fscrypt_key,
                )
            )
        elif args.inline_device:
            mounted.append(
                setup_inline_block_device(
                    label="inline",
                    device=args.inline_device,
                    mount_root=args.inline_mount,
                    mount_opts=args.inline_mount_opts,
                    enc_dir_name=args.inline_enc_dir,
                    key_path=args.fscrypt_key,
                )
            )
        elif args.inline_root:
            mounted.append(
                setup_existing_inline_root(
                    label="inline",
                    root=args.inline_root,
                    enc_dir_name=args.inline_enc_dir,
                    key_path=args.fscrypt_key,
                )
            )

        if args.target_order == "inline-first":
            mounted.sort(key=lambda item: 0 if item.label == "inline" else 1)
        else:
            mounted.sort(key=lambda item: 0 if item.label == "plain" else 1)

        for target in mounted:
            if args.largefolio_worker:
                lf_thread = LargeFolioThread(
                    target,
                    state,
                    file_size_bytes=args.largefolio_file_size,
                    scan_bytes=args.largefolio_scan_size,
                    read_passes=args.largefolio_read_passes,
                    patch_pages=args.largefolio_patch_pages,
                    patch_size_bytes=args.largefolio_patch_size,
                    hotset_bytes=args.largefolio_hotset_size,
                    fsync_every=args.largefolio_fsync_every,
                    verify_every=args.largefolio_verify_every,
                )
                lf_thread.prepare_initial()
                pending_largefolio.append(lf_thread)

            churn = ChurnThread(
                target,
                state,
                garbage_file_bytes=args.garbage_file_size,
                reserve_free_bytes=args.reserve_free,
                headroom_files=args.churn_headroom_files,
                continuous=(args.pressure_mode == "continuous"),
            )
            pending_churn.append(churn)

            gc_thread = GcThread(
                target,
                state,
                gc_window_sec=args.gc_window_sec,
                poll_interval_sec=args.gc_poll_sec,
            )
            pending_gc.append(gc_thread)

        if args.target_file_scheme in {"one", "two"}:
            precreate_case_files(
                mounted,
                cases,
                chunk_size=args.chunk,
                drop_after_prepare=args.drop_after_prepare,
            )
        elif args.target_file_scheme == "alt-half":
            for target in mounted:
                alt_half_runners.append(
                    AlternatingHalfRunner.prepare(
                        target,
                        baseline_kind=args.fixed_baseline_kind,
                        baseline_len=args.fixed_baseline_len,
                        chunk_size=args.chunk,
                        drop_after_prepare=args.drop_after_prepare,
                        config=config,
                        read_passes=args.alt_half_read_passes,
                        delay_sec=args.alt_half_delay_ms / 1000.0,
                    )
                )

        for lf_thread in pending_largefolio:
            lf_thread.start()
            largefolio_threads.append(lf_thread)

        for churn in pending_churn:
            churn.start()
            churn_threads.append(churn)

        for gc_thread in pending_gc:
            gc_thread.start()
            gc_threads.append(gc_thread)

        for churn in churn_threads:
            churn.wait_for_pressure(args.startup_pressure_timeout)

        deadline = time.monotonic() + args.runtime_sec
        targets = [item.to_target_spec() for item in mounted]

        run_count = 0
        print(
            "[INFO] mounted targets="
            + ",".join(f"{item.label}={item.target_dir}" for item in mounted),
            flush=True,
        )
        selected_cases = len(cases) if args.target_file_scheme != "alt-half" else len(alt_half_runners)
        print(f"[INFO] selected_cases={selected_cases} runtime_sec={args.runtime_sec}", flush=True)
        print(
            "[INFO] gc_pressure="
            f"garbage_file_size={args.garbage_file_size} reserve_free={args.reserve_free} "
            f"churn_headroom_files={args.churn_headroom_files} "
            f"gc_window_sec={args.gc_window_sec} gc_poll_sec={args.gc_poll_sec} "
            f"pressure_mode={args.pressure_mode}",
            flush=True,
        )
        print(f"[INFO] target_file_scheme={args.target_file_scheme}", flush=True)
        if args.target_file_scheme == "alt-half":
            print(
                "[INFO] alt_half="
                f"baseline_kind={args.fixed_baseline_kind} baseline_len={args.fixed_baseline_len} "
                f"read_passes={args.alt_half_read_passes} delay_ms={args.alt_half_delay_ms}",
                flush=True,
            )
        if args.largefolio_worker:
            print(
                "[INFO] largefolio_worker="
                f"file_size={args.largefolio_file_size} scan_size={args.largefolio_scan_size} "
                f"read_passes={args.largefolio_read_passes} patch_pages={args.largefolio_patch_pages} "
                f"patch_size={args.largefolio_patch_size} hotset_size={args.largefolio_hotset_size} "
                f"fsync_every={args.largefolio_fsync_every} "
                f"verify_every={args.largefolio_verify_every}",
                flush=True,
            )

        while time.monotonic() < deadline:
            state.raise_if_failed()
            if args.target_file_scheme == "alt-half":
                for runner in alt_half_runners:
                    state.raise_if_failed()
                    verify_mode = args.verify_mode
                    if args.verify_mode == "disk" and args.disk_verify_every > 1:
                        next_run = run_count + 1
                        verify_mode = "disk" if (next_run % args.disk_verify_every) == 0 else "cache"
                    runner.run_step(
                        verify_mode=verify_mode,
                        fsync_after=args.fsync,
                        chunk_size=args.chunk,
                    )
                    run_count += 1
                continue

            for target in targets:
                for case in cases:
                    state.raise_if_failed()
                    verify_mode = args.verify_mode
                    if args.verify_mode == "disk" and args.disk_verify_every > 1:
                        next_run = run_count + 1
                        verify_mode = "disk" if (next_run % args.disk_verify_every) == 0 else "cache"
                    ok = run_matrix_case(
                        case,
                        target,
                        config,
                        verify_mode=verify_mode,
                        fsync_after=args.fsync,
                        read_before_write_passes=args.read_before_write_passes,
                        drop_after_prepare=args.drop_after_prepare,
                        dump=False,
                        block_size=DEFAULT_BLOCK,
                        dump_max=DEFAULT_DUMP_MAX,
                    )
                    run_count += 1
                    if not ok:
                        raise RuntimeError(
                            f"consistency failure: target={target.label} case={case.name} runs={run_count}"
                        )

        print(f"[OK] completed runs={run_count} across {len(targets)} target(s)", flush=True)
        return 0
    except Exception as exc:
        state.fail(str(exc))
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        state.stop_event.set()
        for thread in churn_threads + gc_threads + largefolio_threads:
            join_thread(thread)
        for target in reversed(mounted):
            try:
                cleanup_target(target)
            except Exception as exc:
                print(f"[WARN] cleanup failed for {target.label}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
