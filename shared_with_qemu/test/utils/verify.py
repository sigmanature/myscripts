import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Optional, Tuple

from .patterns import PatternConfig, render_pattern_bytes
from .sysutil import drop_caches


@dataclass(frozen=True)
class MismatchWindow:
    offset: int
    expected: bytes
    actual: bytes


@dataclass(frozen=True)
class OverlaySpec:
    offset: int
    length: int
    config: PatternConfig
    overlay_off: int = 0


@dataclass(frozen=True)
class VerifyEvidenceConfig:
    out_dir: str
    label: str
    save_actual_file: bool = True
    save_expected_file: bool = False
    save_mismatch_window: bool = True
    mismatch_window_bytes: int = 4096


def hex_dump(data: bytes, base: int = 0, bytes_per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(bytes_per_line * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip())
    return cleaned or "verify_failure"


def _pattern_config_dict(config: PatternConfig) -> dict[str, Any]:
    token = config.token or b""
    return {
        "mode": config.mode,
        "token_hex_prefix": token[:64].hex(),
        "token_len": len(token),
        "seed": config.seed,
        "chunk_size": config.chunk_size,
        "pattern_gen": config.pattern_gen,
        "readback": config.readback,
    }


def _overlay_dict(overlay: OverlaySpec) -> dict[str, Any]:
    return {
        "offset": overlay.offset,
        "length": overlay.length,
        "overlay_off": overlay.overlay_off,
        "config": _pattern_config_dict(overlay.config),
    }


def _stat_dict(path: str) -> Optional[dict[str, Any]]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return {
        "inode": st.st_ino,
        "size": st.st_size,
        "mode": st.st_mode,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
    }


def _write_expected_file(
    path: str,
    expected_size: int,
    build_expected: Callable[[int, int], bytes],
    chunk_size: int,
) -> None:
    with open(path, "wb") as fp:
        pos = 0
        while pos < expected_size:
            n = min(chunk_size, expected_size - pos)
            fp.write(build_expected(pos, n))
            pos += n


def save_verify_failure_evidence(
    *,
    path: str,
    expected_size: int,
    evidence: VerifyEvidenceConfig,
    failure: str,
    mismatch_offset: Optional[int],
    expected_window_offset: int = 0,
    expected_window: bytes = b"",
    actual_window: bytes = b"",
    build_expected: Optional[Callable[[int, int], bytes]] = None,
    expected_chunk_size: int = 256 * 1024,
    baseline: Optional[PatternConfig] = None,
    overlays: Iterable[OverlaySpec] = (),
    extra_meta: Optional[dict[str, Any]] = None,
) -> str:
    case_dir = os.path.join(evidence.out_dir, _safe_label(evidence.label))
    os.makedirs(case_dir, exist_ok=True)

    actual_path = os.path.join(case_dir, "actual.bin")
    if evidence.save_actual_file and os.path.exists(path):
        shutil.copy2(path, actual_path)

    if evidence.save_expected_file and build_expected is not None:
        _write_expected_file(
            os.path.join(case_dir, "expected.bin"),
            expected_size,
            build_expected,
            expected_chunk_size,
        )

    if evidence.save_mismatch_window:
        with open(os.path.join(case_dir, "expected.window.bin"), "wb") as fp:
            fp.write(expected_window)
        with open(os.path.join(case_dir, "actual.window.bin"), "wb") as fp:
            fp.write(actual_window)

    ovs = list(overlays)
    meta: dict[str, Any] = {
        "path": path,
        "label": evidence.label,
        "failure": failure,
        "mismatch_offset": mismatch_offset,
        "expected_window_offset": expected_window_offset,
        "expected_window_len": len(expected_window),
        "actual_window_len": len(actual_window),
        "expected_size": expected_size,
        "stat": _stat_dict(path),
        "baseline": _pattern_config_dict(baseline) if baseline is not None else None,
        "overlays": [_overlay_dict(ov) for ov in ovs],
    }
    if extra_meta:
        meta.update(extra_meta)

    with open(os.path.join(case_dir, "verify_meta.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2, sort_keys=True)
        fp.write("\n")

    with open(os.path.join(case_dir, "mismatch.txt"), "w", encoding="utf-8") as fp:
        fp.write(f"failure: {failure}\n")
        fp.write(f"path: {path}\n")
        fp.write(f"expected_size: {expected_size}\n")
        fp.write(f"mismatch_offset: {mismatch_offset}\n")
        fp.write(f"window_offset: {expected_window_offset}\n")
        if expected_window or actual_window:
            fp.write("\nExpected slice:\n")
            fp.write(hex_dump(expected_window, base=expected_window_offset))
            fp.write("\n\nActual slice:\n")
            fp.write(hex_dump(actual_window, base=expected_window_offset))
            fp.write("\n")

    print(f"[verify] evidence saved: {case_dir}", flush=True)
    return case_dir


def verify_stream(
    fd: int,
    file_offset: int,
    size: int,
    expected_direct: Optional[bytes],
    expected_iter: Optional[Iterator[Tuple[int, bytes]]],
    chunk_size: int,
    read_mode: str,
    max_mismatch_show: int = 16,
) -> bool:
    if size <= 0:
        print("[verify] size=0, nothing to compare")
        return True

    def show_mismatch(pos: int, exp: bytes, got: bytes) -> None:
        print(f"\n[FAIL] mismatch at +{pos} (file_off={file_offset + pos})")
        show_len = min(256, len(exp), len(got))
        print("Expected (first up to 256 bytes of this chunk):")
        print(hex_dump(exp[:show_len]))
        print("Actual (first up to 256 bytes of this chunk):")
        print(hex_dump(got[:show_len]))

    if read_mode == "direct":
        got = os.pread(fd, size, file_offset)
        if len(got) != size:
            print(f"[FAIL] readback short: got {len(got)} expected {size}")
            return False
        if expected_direct is None:
            assert expected_iter is not None
            expected_direct = b"".join(chunk for _, chunk in expected_iter)
        if got == expected_direct:
            print("[OK] verify pass (direct compare)")
            return True

        limit = min(len(got), len(expected_direct))
        mismatches = 0
        for i in range(limit):
            if got[i] != expected_direct[i]:
                lo = max(0, i - 64)
                hi = min(limit, i + 64)
                show_mismatch(i, expected_direct[lo:hi], got[lo:hi])
                mismatches += 1
                if mismatches >= max_mismatch_show:
                    break
        return False

    if expected_direct is not None:
        pos = 0
        while pos < size:
            n = min(chunk_size, size - pos)
            got = os.pread(fd, n, file_offset + pos)
            exp = expected_direct[pos:pos + n]
            if got != exp:
                show_mismatch(pos, exp, got)
                return False
            pos += n
        print("[OK] verify pass (stream compare vs direct expected)")
        return True

    assert expected_iter is not None
    for rel_pos, exp in expected_iter:
        got = os.pread(fd, len(exp), file_offset + rel_pos)
        if got != exp:
            show_mismatch(rel_pos, exp, got)
            return False
    print("[OK] verify pass (stream compare)")
    return True


def fill_largefolio_pattern(fd: int, size: int, span: int, chunk_size: int = 64 * 1024) -> None:
    if size < 0 or span < 0:
        raise ValueError("size/span must be >= 0")
    os.ftruncate(fd, size)
    chunk = bytes((i ^ 0x5A) & 0xFF for i in range(chunk_size))
    off = 0
    while off < span:
        take = min(len(chunk), span - off)
        wrote = os.pwrite(fd, chunk[:take], off)
        if wrote != take:
            raise OSError(errno.EIO, f"pwrite short: wrote {wrote} expected {take}")
        off += take
    os.fsync(fd)


def build_expected_full_chunk(
    start: int,
    length: int,
    baseline_kind: str,
    baseline_len: int,
    overlay_off: int,
    overlay_len: int,
    config: PatternConfig,
) -> bytes:
    exp = bytearray(length)

    if baseline_kind == "existing_a":
        baseline_end = min(start + length, baseline_len)
        if baseline_end > start:
            fill_len = baseline_end - start
            exp[:fill_len] = b"A" * fill_len
    elif baseline_kind != "hole":
        raise ValueError(f"unknown baseline kind: {baseline_kind}")

    overlay_end = overlay_off + overlay_len
    seg_start = max(start, overlay_off)
    seg_end = min(start + length, overlay_end)
    if seg_start < seg_end:
        rel = seg_start - start
        seg_len = seg_end - seg_start
        exp[rel:rel + seg_len] = render_pattern_bytes(
            seg_start,
            seg_len,
            config,
            overlay_off=overlay_off,
        )

    return bytes(exp)


def first_mismatch_window(
    fd: int,
    expected_len: int,
    chunk_start: int,
    expected: bytes,
    actual: bytes,
    baseline_kind: str,
    baseline_len: int,
    overlay_off: int,
    overlay_len: int,
    config: PatternConfig,
) -> MismatchWindow:
    for idx in range(min(len(expected), len(actual))):
        if expected[idx] != actual[idx]:
            mismatch_off = chunk_start + idx
            lo = max(0, mismatch_off - 64)
            hi = min(expected_len, mismatch_off + 64)
            got = os.pread(fd, hi - lo, lo)
            exp = build_expected_full_chunk(
                lo,
                hi - lo,
                baseline_kind,
                baseline_len,
                overlay_off,
                overlay_len,
                config,
            )
            return MismatchWindow(offset=lo, expected=exp, actual=got)

    mismatch_off = chunk_start
    lo = max(0, mismatch_off - 64)
    hi = min(expected_len, mismatch_off + 64)
    got = os.pread(fd, hi - lo, lo)
    exp = build_expected_full_chunk(
        lo,
        hi - lo,
        baseline_kind,
        baseline_len,
        overlay_off,
        overlay_len,
        config,
    )
    return MismatchWindow(offset=lo, expected=exp, actual=got)


def verify_full_overlay(
    path: str,
    baseline_kind: str,
    baseline_len: int,
    overlay_off: int,
    overlay_len: int,
    expected_len: int,
    config: PatternConfig,
    *,
    cold_read: bool,
) -> bool:
    if cold_read:
        print("[verify] full-file disk-mode: os.sync + drop_caches(3) + reopen")
        try:
            t0 = time.perf_counter()
            drop_caches(3)
            t1 = time.perf_counter()
            print(f"[verify] drop_caches done in {t1 - t0:.3f}s")
        except PermissionError:
            print("[FAIL] drop_caches requires root (permission denied).")
            return False
        except Exception as exc:
            print(f"[FAIL] drop_caches error: {exc}")
            return False

    try:
        st = os.stat(path)
    except FileNotFoundError:
        print(f"[FAIL] file not found: {path}")
        return False

    if st.st_size != expected_len:
        print(f"[FAIL] size mismatch: actual={st.st_size} expected={expected_len}")
        return False

    fd = None
    try:
        fd = os.open(path, os.O_RDONLY)
        pos = 0
        while pos < expected_len:
            n = min(config.chunk_size, expected_len - pos)
            got = os.pread(fd, n, pos)
            if len(got) != n:
                print(f"[FAIL] short read at {pos}: got {len(got)} expect {n}")
                return False

            exp = build_expected_full_chunk(
                pos,
                n,
                baseline_kind,
                baseline_len,
                overlay_off,
                overlay_len,
                config,
            )
            if got != exp:
                window = first_mismatch_window(
                    fd,
                    expected_len,
                    pos,
                    exp,
                    got,
                    baseline_kind,
                    baseline_len,
                    overlay_off,
                    overlay_len,
                    config,
                )
                print(f"[FAIL] full-file mismatch around offset {window.offset}")
                print("Expected slice:")
                print(hex_dump(window.expected, base=window.offset))
                print("Actual slice:")
                print(hex_dump(window.actual, base=window.offset))
                return False
            pos += n

        print("[OK] full-file verify pass")
        return True
    finally:
        if fd is not None:
            os.close(fd)


def verify_file_overlays(
    path: str,
    *,
    expected_size: int,
    baseline: PatternConfig,
    overlays: Iterable[OverlaySpec],
    chunk_size: int = 256 * 1024,
    cold_read: bool = True,
    evidence: Optional[VerifyEvidenceConfig] = None,
) -> bool:
    """Verify full file content on disk as baseline overridden by overlays."""
    if expected_size < 0:
        raise ValueError("expected_size must be >= 0")

    ovs = list(overlays)

    def build_expected(pos: int, n: int) -> bytes:
        exp = bytearray(render_pattern_bytes(pos, n, baseline, overlay_off=0))
        for ov in ovs:
            if ov.length <= 0:
                continue
            ov_end = ov.offset + ov.length
            seg_start = max(pos, ov.offset)
            seg_end = min(pos + n, ov_end)
            if seg_start >= seg_end:
                continue
            rel = seg_start - pos
            seg_len = seg_end - seg_start
            exp[rel:rel + seg_len] = render_pattern_bytes(
                seg_start,
                seg_len,
                ov.config,
                overlay_off=ov.overlay_off,
            )
        return bytes(exp)

    def save_evidence(
        failure: str,
        mismatch_offset: Optional[int],
        window_offset: int = 0,
        expected_window: bytes = b"",
        actual_window: bytes = b"",
    ) -> None:
        if evidence is None:
            return
        save_verify_failure_evidence(
            path=path,
            expected_size=expected_size,
            evidence=evidence,
            failure=failure,
            mismatch_offset=mismatch_offset,
            expected_window_offset=window_offset,
            expected_window=expected_window,
            actual_window=actual_window,
            build_expected=build_expected,
            expected_chunk_size=chunk_size,
            baseline=baseline,
            overlays=ovs,
        )

    if cold_read:
        try:
            os.sync()
            drop_caches(3)
        except Exception as exc:
            print(f"[FAIL] drop_caches error: {exc}")
            return False

    try:
        st = os.stat(path)
    except FileNotFoundError:
        print(f"[FAIL] file not found: {path}")
        save_evidence("file_not_found", None)
        return False

    if st.st_size != expected_size:
        print(f"[FAIL] size mismatch: actual={st.st_size} expected={expected_size}")
        save_evidence("size_mismatch", None)
        return False

    fd = os.open(path, os.O_RDONLY)
    try:
        pos = 0
        while pos < expected_size:
            n = min(chunk_size, expected_size - pos)
            got = os.pread(fd, n, pos)
            if len(got) != n:
                print(f"[FAIL] short read at {pos}: got {len(got)} expect {n}")
                save_evidence("short_read", pos, pos, build_expected(pos, n), got)
                return False

            exp = build_expected(pos, n)

            if got != exp:
                # Find first mismatch to print a tight window.
                idx = next((i for i in range(n) if got[i] != exp[i]), 0)
                half_window = max(64, evidence.mismatch_window_bytes // 2) if evidence else 64
                win_off = max(0, pos + idx - half_window)
                win_end = min(expected_size, pos + idx + half_window)
                got_win = os.pread(fd, win_end - win_off, win_off)
                exp_win = build_expected(win_off, win_end - win_off)
                print(f"[FAIL] mismatch around offset {win_off}")
                print("Expected slice:")
                print(hex_dump(exp_win, base=win_off))
                print("Actual slice:")
                print(hex_dump(got_win, base=win_off))
                save_evidence("mismatch", pos + idx, win_off, exp_win, got_win)
                return False

            pos += n

        print("[OK] disk verify pass")
        return True
    finally:
        os.close(fd)
