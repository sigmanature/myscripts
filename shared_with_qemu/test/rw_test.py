#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Buffered I/O and scenario-driven rw test harness.

Goals:
- Keep the old low-level `read` / `write` / `verify` CLI behavior.
- Provide reusable provider functions for shell wrappers.
- Move matrix scenario execution and full-file overlay verification into Python.
- Support read-then-write matrix variants as a first-class mode.
"""

from __future__ import annotations

import argparse
import errno
import mmap
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Sequence, Tuple


DEFAULT_TOKEN = "PyWrtDta"
DEFAULT_CHUNK = 1024 * 1024
DEFAULT_BLOCK = 4096
DEFAULT_DUMP_MAX = 256 * 1024
PAGE = 4096
FOLIO_16K = 16 * 1024
TARGET_LARGE = 64 * 1024
MMAP_FILE_SZ = 8 * 1024 * 1024
RA_SPAN = 2 * 1024 * 1024


BASE_A = 64 * 1024
BASE_U = 65537
HOLE_A = 64 * 1024

OVERWRITE_LAYOUTS = (
    ("o0_aligned", 0, 64 * 1024),
    ("o0_unaligned", 0, 65535),
    ("onz_aligned", 8 * 1024, 56 * 1024),
    ("onz_unaligned", 4097, 65536 - 4097),
)

APPEND_LAYOUTS = (
    ("baseAligned_szAligned", BASE_A, 64 * 1024),
    ("baseAligned_szUnaligned", BASE_A, 65535),
    ("baseUnaligned_szAligned", BASE_U, 64 * 1024),
    ("baseUnaligned_szUnaligned", BASE_U, 65535),
)


@dataclass(frozen=True)
class PatternConfig:
    mode: str
    token: bytes
    seed: int
    chunk_size: int
    pattern_gen: str
    readback: str


@dataclass(frozen=True)
class MatrixCase:
    name: str
    baseline_kind: str
    baseline_len: int
    write_style: str
    offset: int
    size: int
    read_before_write: bool = False

    def baseline_tag(self) -> str:
        return "existA" if self.baseline_kind == "existing_a" else "hole"

    def op_tag(self) -> str:
        if self.read_before_write:
            return f"read_then_{self.write_style}"
        return self.write_style

    def file_name(self, target_label: str) -> str:
        return f"{target_label}_{self.baseline_tag()}_{self.op_tag()}_{self.name}.bin"

    def run_tag(self, target_label: str) -> str:
        return f"{target_label}/{self.baseline_tag()}/{self.op_tag()}/{self.name}"


@dataclass(frozen=True)
class TargetSpec:
    label: str
    directory: str


@dataclass(frozen=True)
class MismatchWindow:
    offset: int
    expected: bytes
    actual: bytes


@dataclass(frozen=True)
class MmapBuiltinCase:
    name: str
    file_name: str
    expected_mkwrite: Optional[int]
    pre_drop_caches: bool = False


def parse_bytes(size_str: str) -> int:
    """Parse '123', '4k', '10m', '1g', '512b' into bytes."""
    s = str(size_str).strip().lower()
    if not s:
        raise ValueError("size string cannot be empty")

    unit = s[-1]
    if unit.isalpha():
        num_part = s[:-1]
        factors = {
            "b": 1,
            "k": 1024,
            "m": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
        }
        if unit not in factors:
            raise ValueError(f"invalid unit '{unit}' in '{s}'")
        factor = factors[unit]
    else:
        num_part = s
        factor = 1

    if not num_part.isdigit():
        if len(s) == 1 and s.isalpha():
            raise ValueError(f"missing number before unit in '{s}'")
        raise ValueError(f"invalid number part '{num_part}' in '{s}'")

    return int(num_part) * factor


def hex_dump(data: bytes, base: int = 0, bytes_per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(bytes_per_line * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def dump_aligned_blocks(
    filename: str,
    offset_bytes: int,
    size_bytes: int,
    block_size: int = DEFAULT_BLOCK,
    max_dump_bytes: int = 64 * 1024,
    title: str = "",
) -> None:
    """Dump block-aligned region intersecting [offset, offset+size)."""
    if size_bytes <= 0:
        print(f"[dump] size=0, skip. {title}".strip())
        return

    start = (offset_bytes // block_size) * block_size
    end = ((offset_bytes + size_bytes + block_size - 1) // block_size) * block_size
    total = end - start

    if total <= 0:
        print(f"[dump] nothing to dump. {title}".strip())
        return

    print("\n" + "=" * 70)
    if title:
        print(f"[Block-aligned dump] {title}")
    print(f"block_size={block_size}  aligned_range=[{start}, {end})  total={total} bytes")
    print(f"target_range=[{offset_bytes}, {offset_bytes + size_bytes})")

    if total > max_dump_bytes:
        print(
            f"(Output truncated) total {total} > max_dump_bytes {max_dump_bytes}, "
            f"only dumping first {max_dump_bytes} bytes."
        )
        end = start + max_dump_bytes
        total = max_dump_bytes

    try:
        with open(filename, "rb") as f:
            f.seek(start)
            data = f.read(total)
    except Exception as exc:
        print(f"[dump] error reading file for dump: {exc}")
        print("=" * 70)
        return

    print(f"(Base offset shown below is relative; add {start} to get file offsets)")
    print(hex_dump(data, base=start))
    print("=" * 70 + "\n")


def _repeat_bytes(token: bytes, start_idx: int, n: int) -> bytes:
    if n <= 0:
        return b""
    if not token:
        raise ValueError("pattern token cannot be empty")

    token_len = len(token)
    start_idx %= token_len
    out = bytearray(n)

    first = min(n, token_len - start_idx)
    out[:first] = token[start_idx:start_idx + first]
    filled = first
    while filled < n:
        take = min(token_len, n - filled)
        out[filled:filled + take] = token[:take]
        filled += take
    return bytes(out)


def render_pattern_bytes(abs_pos: int, n: int, config: PatternConfig, overlay_off: int = 0) -> bytes:
    if config.mode == "counter":
        base = config.seed + abs_pos
        return bytes(((base + i) & 0xFF) for i in range(n))
    if config.mode == "filepos":
        return _repeat_bytes(config.token, abs_pos, n)
    if config.mode == "repeat":
        return _repeat_bytes(config.token, abs_pos - overlay_off, n)
    raise ValueError(f"unknown pattern mode: {config.mode}")


def iter_expected_chunks(
    offset: int,
    size: int,
    config: PatternConfig,
) -> Iterator[Tuple[int, bytes]]:
    """Yield (relative_pos, expected_chunk) for [offset, offset+size)."""
    pos = 0
    while pos < size:
        chunk = min(config.chunk_size, size - pos)
        yield pos, render_pattern_bytes(offset + pos, chunk, config)
        pos += chunk


def generate_expected_direct(offset: int, size: int, config: PatternConfig) -> bytes:
    parts = []
    for _, chunk in iter_expected_chunks(offset, size, config):
        parts.append(chunk)
    return b"".join(parts)


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)


def ensure_file_exists(path: str) -> bool:
    preexisted = os.path.exists(path)
    if not preexisted:
        ensure_parent_dir(path)
        open(path, "ab").close()
    return preexisted


def ensure_size_sparse(path: str, required_size: int) -> None:
    if required_size <= 0:
        return
    ensure_file_exists(path)
    st = os.stat(path)
    if st.st_size >= required_size:
        return
    with open(path, "r+b") as f:
        os.ftruncate(f.fileno(), required_size)


def truncate_sparse(path: str, size: int) -> None:
    ensure_parent_dir(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def drop_caches(level: int) -> None:
    if level not in (1, 2, 3):
        raise ValueError("drop_caches level must be 1/2/3")
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as f:
        f.write(str(level))


def best_effort_drop_caches(level: int = 3) -> bool:
    try:
        drop_caches(level)
        return True
    except Exception:
        return False


def open_fd_for_write(path: str) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def open_fd_for_read(path: str) -> int:
    return os.open(path, os.O_RDONLY)


def write_file_bytes(
    path: str,
    data: bytes,
    *,
    truncate: bool = True,
    fsync_after: bool = True,
) -> None:
    ensure_parent_dir(path)
    flags = os.O_RDWR | os.O_CREAT
    if truncate:
        flags |= os.O_TRUNC

    fd = os.open(path, flags, 0o644)
    try:
        view = memoryview(data)
        pos = 0
        while pos < len(view):
            written = os.write(fd, view[pos:])
            if written <= 0:
                raise OSError("short write while writing full file bytes")
            pos += written
        if fsync_after:
            os.fsync(fd)
    finally:
        os.close(fd)


def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def pwrite_all(fd: int, file_offset: int, data_iter: Iterator[Tuple[int, bytes]]) -> int:
    total = 0
    for rel_pos, chunk in data_iter:
        if not chunk:
            continue
        written = os.pwrite(fd, chunk, file_offset + rel_pos)
        total += written
        if written != len(chunk):
            raise OSError(f"short pwrite: wrote {written} expected {len(chunk)}")
    return total


def build_pattern_source(
    offset: int,
    size: int,
    config: PatternConfig,
) -> Tuple[Optional[bytes], Iterator[Tuple[int, bytes]]]:
    if config.pattern_gen == "direct":
        print(f"[pattern] generating expected DIRECT into memory: {size} bytes")
        t0 = time.perf_counter()
        direct = generate_expected_direct(offset, size, config)
        t1 = time.perf_counter()
        print(f"[pattern] direct generation done in {t1 - t0:.3f}s")
        return direct, iter(((0, direct),))
    return None, iter_expected_chunks(offset, size, config)


def do_write_region(
    path: str,
    offset: int,
    size: int,
    config: PatternConfig,
    *,
    fsync_after: bool,
    dump: bool = False,
    block_size: int = DEFAULT_BLOCK,
    dump_max: int = DEFAULT_DUMP_MAX,
) -> bool:
    ensure_parent_dir(path)
    ensure_file_exists(path)
    ensure_size_sparse(path, offset + size)

    if dump:
        dump_aligned_blocks(
            path,
            offset,
            size,
            block_size=block_size,
            max_dump_bytes=dump_max,
            title="BEFORE write",
        )

    _, source_iter = build_pattern_source(offset, size, config)

    fd = None
    try:
        fd = open_fd_for_write(path)
        written = pwrite_all(fd, offset, source_iter)
        print(f"[write] wrote {written} bytes")
        if fsync_after:
            os.fsync(fd)
            print("[write] fsync done")
        return True
    except Exception as exc:
        print(f"[FAIL] write error: {exc}")
        return False
    finally:
        if fd is not None:
            os.close(fd)
        if dump:
            dump_aligned_blocks(
                path,
                offset,
                size,
                block_size=block_size,
                max_dump_bytes=dump_max,
                title="AFTER write",
            )


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


def read_region_discard(path: str, offset: int, size: int, chunk_size: int, passes: int = 1) -> int:
    if size <= 0 or passes <= 0:
        return 0

    fd = None
    try:
        fd = open_fd_for_read(path)
        return read_fd_discard(fd, offset, size, chunk_size, passes=passes)
    finally:
        if fd is not None:
            os.close(fd)


def read_fd_discard(fd: int, offset: int, size: int, chunk_size: int, passes: int = 1) -> int:
    if size <= 0 or passes <= 0:
        return 0

    total = 0
    for _ in range(passes):
        pos = 0
        while pos < size:
            chunk = min(chunk_size, size - pos)
            data = os.pread(fd, chunk, offset + pos)
            if not data:
                break
            total += len(data)
            pos += len(data)
    return total


def round_down(x: int, align: int) -> int:
    return x & ~(align - 1)


def stat_inode(path: str) -> int:
    return os.stat(path).st_ino


def dump_bytes(label: str, data: bytes, start: int, length: int = 16) -> None:
    end = min(len(data), start + length)
    chunk = data[start:end]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    print(f"{label}: off={start} len={len(chunk)} bytes=[{hexs}]", flush=True)


def assert_bytes_equal(actual: bytes, expected: bytes, context: str) -> None:
    if actual == expected:
        return
    mismatch = next((i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]), None)
    if mismatch is None:
        raise AssertionError(f"{context}: length mismatch actual={len(actual)} expected={len(expected)}")
    lo = max(0, mismatch - 32)
    hi = min(min(len(actual), len(expected)), mismatch + 32)
    raise AssertionError(
        f"{context}: mismatch at offset {mismatch}\n"
        f"expected:\n{hex_dump(expected[lo:hi], base=lo)}\n"
        f"actual:\n{hex_dump(actual[lo:hi], base=lo)}"
    )


def expected_mod251_pattern(size: int) -> bytearray:
    out = bytearray(size)
    for i in range(size):
        out[i] = i % 251
    return out


def mmap_shared_write(
    path: str,
    length: int,
    writes: Sequence[Tuple[int, bytes]],
    *,
    read_before_write_offsets: Sequence[int] = (),
    flush_range: Optional[Tuple[int, int]] = None,
    pdb_before_write: bool = False,
) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        mm = mmap.mmap(fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            for off in read_before_write_offsets:
                _ = mm[off]

            if pdb_before_write:
                import pdb
                pdb.set_trace()

            for off, data in writes:
                mm[off:off + len(data)] = data

            if flush_range is None:
                mm.flush()
            else:
                mm.flush(flush_range[0], flush_range[1])
        finally:
            mm.close()
        os.fsync(fd)
    finally:
        os.close(fd)


def cold_read_file_bytes(path: str, *, drop_first: bool) -> bytes:
    if drop_first:
        drop_caches(3)
    return read_file_bytes(path)


def posix_fallocate_or_truncate(fd: int, size: int) -> None:
    try:
        os.posix_fallocate(fd, 0, size)
    except AttributeError:
        os.ftruncate(fd, size)
    except OSError:
        os.ftruncate(fd, size)




def fill_largefolio_pattern(fd: int, size: int, span: int) -> None:
    posix_fallocate_or_truncate(fd, size)
    chunk = bytes((i ^ 0x5A) & 0xFF for i in range(TARGET_LARGE))

    def iter_fill_chunks() -> Iterator[Tuple[int, bytes]]:
        off = 0
        while off < span:
            take = min(len(chunk), span - off)
            yield off, chunk[:take]
            off += take

    written = pwrite_all(fd, 0, iter_fill_chunks())
    if written != span:
        raise OSError(errno.EIO, f"pwrite short: wrote {written} expected {span}")
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
        exp[rel:rel + seg_len] = render_pattern_bytes(seg_start, seg_len, config, overlay_off=overlay_off)

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
        fd = open_fd_for_read(path)
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


def prepare_baseline(
    path: str,
    baseline_kind: str,
    baseline_len: int,
    *,
    chunk_size: int,
    drop_after_prepare: bool,
) -> bool:
    truncate_sparse(path, baseline_len)

    if baseline_kind == "existing_a":
        fill_config = PatternConfig(
            mode="repeat",
            token=b"A",
            seed=0,
            chunk_size=chunk_size,
            pattern_gen="stream",
            readback="stream",
        )
        if not do_write_region(
            path,
            0,
            baseline_len,
            fill_config,
            fsync_after=True,
        ):
            return False
    elif baseline_kind != "hole":
        print(f"[FAIL] unknown baseline kind: {baseline_kind}")
        return False

    if drop_after_prepare:
        try:
            drop_caches(3)
            print("[prep] drop_caches after baseline prepare done")
        except PermissionError:
            print("[WARN] skip drop_caches after baseline prepare: permission denied")
        except Exception as exc:
            print(f"[WARN] skip drop_caches after baseline prepare: {exc}")

    return True


def resolve_write_offset(case: MatrixCase) -> int:
    if case.write_style == "append":
        return case.baseline_len
    return case.offset


def resolve_expected_len(case: MatrixCase, write_offset: int) -> int:
    return max(case.baseline_len, write_offset + case.size)


def warmup_range_for_case(case: MatrixCase, write_offset: int) -> Tuple[int, int]:
    if case.write_style == "append":
        return 0, case.baseline_len
    return write_offset, case.size


def run_matrix_case(
    case: MatrixCase,
    target: TargetSpec,
    config: PatternConfig,
    *,
    verify_mode: str,
    fsync_after: bool,
    read_before_write_passes: int,
    drop_after_prepare: bool,
    dump: bool,
    block_size: int,
    dump_max: int,
) -> bool:
    path = os.path.join(target.directory, case.file_name(target.label))
    tag = case.run_tag(target.label)

    print()
    print("================================================================")
    print(f"[RUN] {tag}")
    print(f"      file={path}")
    print(
        "      "
        f"baseline_kind={case.baseline_kind} baseline_len={case.baseline_len} "
        f"write_style={case.write_style} offset={case.offset} size={case.size} "
        f"read_before_write={case.read_before_write}"
    )
    print("================================================================")

    if not prepare_baseline(
        path,
        case.baseline_kind,
        case.baseline_len,
        chunk_size=config.chunk_size,
        drop_after_prepare=drop_after_prepare,
    ):
        return False

    write_offset = resolve_write_offset(case)
    expected_len = resolve_expected_len(case, write_offset)

    if case.read_before_write:
        read_off, read_len = warmup_range_for_case(case, write_offset)
        if read_len > 0 and read_before_write_passes > 0:
            total = read_region_discard(path, read_off, read_len, config.chunk_size, read_before_write_passes)
            print(
                "[warmup] read-before-write done "
                f"offset={read_off} size={read_len} passes={read_before_write_passes} total={total}"
            )
        else:
            print("[warmup] read-before-write skipped because readable span is 0")

    if not do_write_region(
        path,
        write_offset,
        case.size,
        config,
        fsync_after=fsync_after,
        dump=dump,
        block_size=block_size,
        dump_max=dump_max,
    ):
        return False

    ok = verify_full_overlay(
        path,
        case.baseline_kind,
        case.baseline_len,
        write_offset,
        case.size,
        expected_len,
        config,
        cold_read=(verify_mode == "disk"),
    )
    if ok:
        print(f"[OK ] {tag}")
    return ok


def make_case(
    name: str,
    baseline_kind: str,
    baseline_len: int,
    write_style: str,
    offset: int,
    size: int,
    *,
    read_before_write: bool = False,
) -> MatrixCase:
    return MatrixCase(
        name=name,
        baseline_kind=baseline_kind,
        baseline_len=baseline_len,
        write_style=write_style,
        offset=offset,
        size=size,
        read_before_write=read_before_write,
    )


def build_builtin_matrix_cases(include_read_then_write: bool) -> list[MatrixCase]:
    cases: list[MatrixCase] = []
    baseline_matrix = (
        ("existing_a", BASE_A),
        ("hole", HOLE_A),
    )

    for baseline_kind, overwrite_base_len in baseline_matrix:
        for name, off, size in OVERWRITE_LAYOUTS:
            cases.append(
                make_case(
                    name,
                    baseline_kind,
                    overwrite_base_len,
                    "overwrite",
                    off,
                    size,
                )
            )
            if include_read_then_write:
                cases.append(
                    make_case(
                        name,
                        baseline_kind,
                        overwrite_base_len,
                        "overwrite",
                        off,
                        size,
                        read_before_write=True,
                    )
                )

    for baseline_kind in ("existing_a", "hole"):
        for name, baseline_len, size in APPEND_LAYOUTS:
            cases.append(
                make_case(
                    name,
                    baseline_kind,
                    baseline_len,
                    "append",
                    0,
                    size,
                )
            )
            if include_read_then_write:
                cases.append(
                    make_case(
                        name,
                        baseline_kind,
                        baseline_len,
                        "append",
                        0,
                        size,
                        read_before_write=True,
                    )
                )

    return cases


def filter_matrix_cases(
    cases: Sequence[MatrixCase],
    baseline_filters: Sequence[str],
    write_style_filters: Sequence[str],
    case_filters: Sequence[str],
    include_read_then_write: bool,
) -> list[MatrixCase]:
    out = []
    for case in cases:
        if baseline_filters and case.baseline_kind not in baseline_filters:
            continue
        if write_style_filters and case.write_style not in write_style_filters:
            continue
        if not include_read_then_write and case.read_before_write:
            continue
        if case_filters:
            hay = f"{case.name} {case.run_tag('target')}".lower()
            if not any(pattern.lower() in hay for pattern in case_filters):
                continue
        out.append(case)
    return out


def build_builtin_mmap_cases(include_wp_subpage: bool = True) -> list[MmapBuiltinCase]:
    cases = [
        MmapBuiltinCase("t1_single_page", "t1_single_page.bin", 1, pre_drop_caches=False),
        MmapBuiltinCase("t2_4pages_samefolio", "t2_4pages_samefolio.bin", 4, pre_drop_caches=True),
        MmapBuiltinCase("t3_cross_folio", "t3_cross_folio.bin", 2, pre_drop_caches=True),
        MmapBuiltinCase("t4_sigbus_past_eof", "t4_sigbus.bin", 1, pre_drop_caches=True),
        MmapBuiltinCase("t5_tail_zeroing_off_in_folio", "t5_tail_zeroing.bin", 1, pre_drop_caches=False),
    ]
    if include_wp_subpage:
        cases.append(
            MmapBuiltinCase(
                "wp_subpage_read_then_write",
                "wp_subpage_read_then_write.bin",
                1,
                pre_drop_caches=False,
            )
        )
    return cases


def find_builtin_mmap_case(name: str, include_wp_subpage: bool = True) -> MmapBuiltinCase:
    for case in build_builtin_mmap_cases(include_wp_subpage=include_wp_subpage):
        if case.name == name:
            return case
    raise KeyError(name)


def filter_builtin_mmap_cases(
    cases: Sequence[MmapBuiltinCase],
    name_filters: Sequence[str],
) -> list[MmapBuiltinCase]:
    if not name_filters:
        return list(cases)
    out = []
    for case in cases:
        hay = case.name.lower()
        if any(pattern.lower() in hay for pattern in name_filters):
            out.append(case)
    return out


def run_builtin_mmap_case(
    case: MmapBuiltinCase,
    path: str,
    *,
    drop_caches_override: Optional[bool] = None,
    do_readahead: bool = False,
    pdb_before_write: bool = False,
) -> Tuple[str, Optional[int]]:
    ensure_parent_dir(path)

    pre_drop = case.pre_drop_caches if drop_caches_override is None else drop_caches_override
    if pre_drop:
        drop_caches(3)
        print("[prep] drop_caches before mmap case done")

    if case.name == "t1_single_page":
        size = 64 * 1024
        base = expected_mod251_pattern(size)
        write_file_bytes(path, bytes(base), truncate=True, fsync_after=True)
        print(f"TEST t1 start path={path} inode={stat_inode(path)} size={size}")
        dump_bytes("TEST t1 base-before", base, 112)
        off = 123
        base[off] = 0x5A
        dump_bytes("TEST t1 base-after", base, 112)
        mmap_shared_write(path, size, ((off, b"\x5A"),))
        got = cold_read_file_bytes(path, drop_first=True)
        assert_bytes_equal(got, bytes(base), "t1 disk content")
        return path, case.expected_mkwrite

    if case.name == "t2_4pages_samefolio":
        size = 64 * 1024
        base = expected_mod251_pattern(size)
        write_file_bytes(path, bytes(base), truncate=True, fsync_after=True)
        print(f"TEST t2 start path={path} inode={stat_inode(path)} size={size}")
        for off in (0, PAGE, 2 * PAGE, 3 * PAGE):
            dump_bytes(f"TEST t2 base-before@{off}", base, off)

        writes = []
        for idx, off in enumerate((0, PAGE, 2 * PAGE, 3 * PAGE)):
            base[off] = (0xA0 + idx) & 0xFF
            writes.append((off, bytes([base[off]])))
        print(f"TEST t2 writes={[(off, bs.hex()) for off, bs in writes]}")
        for off in (0, PAGE, 2 * PAGE, 3 * PAGE):
            dump_bytes(f"TEST t2 base-after@{off}", base, off)
        mmap_shared_write(path, size, tuple(writes))
        got = cold_read_file_bytes(path, drop_first=True)
        assert_bytes_equal(got, bytes(base), "t2 disk content")
        return path, case.expected_mkwrite

    if case.name == "t3_cross_folio":
        size = 128 * 1024
        base = expected_mod251_pattern(size)
        write_file_bytes(path, bytes(base), truncate=True, fsync_after=True)
        print(f"TEST t3 start path={path} inode={stat_inode(path)} size={size}")
        off = FOLIO_16K - 4
        patch = bytes([0xEE] * 16)
        dump_bytes("TEST t3 base-before", base, off, 24)
        base[off:off + 16] = patch
        dump_bytes("TEST t3 base-after", base, off, 24)
        mmap_shared_write(path, size, ((off, patch),))
        got = cold_read_file_bytes(path, drop_first=True)
        assert_bytes_equal(got, bytes(base), "t3 disk content")
        return path, case.expected_mkwrite

    if case.name == "t4_sigbus_past_eof":
        size = PAGE
        write_file_bytes(path, b"\x11" * size, truncate=True, fsync_after=True)
        print(f"TEST t4 start path={path} inode={stat_inode(path)} size={size}")
        helper = (
            "import mmap, os, sys\n"
            f"path = {path!r}\n"
            f"orig = {size}\n"
            f"mapsz = {size + PAGE}\n"
            "fd = os.open(path, os.O_RDWR)\n"
            "try:\n"
            "    os.ftruncate(fd, mapsz)\n"
            "    mm = mmap.mmap(fd, mapsz, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)\n"
            "    os.ftruncate(fd, orig)\n"
            "    mm[orig] = 0x22\n"
            "    mm.flush()\n"
            "    mm.close()\n"
            "    os.fsync(fd)\n"
            "finally:\n"
            "    os.close(fd)\n"
            "sys.exit(0)\n"
        )
        cp = subprocess.run([sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cp.returncode != -signal.SIGBUS:
            raise AssertionError(
                f"t4 expected SIGBUS (returncode {-signal.SIGBUS}), got {cp.returncode}, stderr={cp.stderr!r}"
            )
        return path, case.expected_mkwrite

    if case.name == "t5_tail_zeroing_off_in_folio":
        write_file_bytes(path, b"\xCC" * 8192, truncate=True, fsync_after=True)
        print(f"TEST t5 start path={path} inode={stat_inode(path)} size=8192")

        fd = os.open(path, os.O_RDWR)
        try:
            os.ftruncate(fd, 5000)
            os.fsync(fd)
        finally:
            os.close(fd)

        mmap_shared_write(path, 8192, ((4096, b"\xAB"),))

        fd = os.open(path, os.O_RDWR)
        try:
            os.ftruncate(fd, 8192)
            os.fsync(fd)
        finally:
            os.close(fd)

        got = cold_read_file_bytes(path, drop_first=True)
        dump_bytes("TEST t5 readback@4096", got, 4096, 32)
        tail = got[5000:8192]
        if tail != b"\x00" * len(tail):
            nz = next((i for i, b in enumerate(tail) if b != 0), None)
            raise AssertionError(f"t5 tail not zero at +{nz} (abs off {5000 + nz}), val=0x{tail[nz]:02x}")
        return path, case.expected_mkwrite

    if case.name == "wp_subpage_read_then_write":
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fill_largefolio_pattern(fd, MMAP_FILE_SZ, RA_SPAN)

            if drop_caches_override:
                ok = best_effort_drop_caches(3)
                print(f"[*] drop_caches: {'OK' if ok else 'SKIP/FAIL (need root?)'}")

            if do_readahead:
                print("[*] --do-readahead is deprecated; relying on sequential pread scan.")

            print("[*] sequential pread scan (2 passes) to ramp readahead...")
            read_fd_discard(fd, 0, RA_SPAN, 256 * 1024, passes=2)

            mm = mmap.mmap(fd, MMAP_FILE_SZ, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
            try:
                base = round_down(512 * 1024, TARGET_LARGE)
                target = base + 7 * PAGE
                if target + PAGE > MMAP_FILE_SZ:
                    raise RuntimeError("target out of range")
                print(f"[*] target offset = {target} (base {base}, subpage {7}/16 of 64K window)")

                old = mm[target]
                if pdb_before_write:
                    import pdb
                    pdb.set_trace()
                newv = (old ^ 0x01) & 0xFF
                mm[target] = newv
                mm.flush(target, PAGE)
                os.fsync(fd)

                disk = os.pread(fd, 1, target)
                if len(disk) != 1 or disk[0] != newv:
                    raise AssertionError(
                        f"wp_subpage disk mismatch at {target}: got {disk[0] if disk else None} expected {newv}"
                    )
                print(f"PASS: wrote 1 byte at {target}: {old:#04x} -> {newv:#04x}, flush+fsync persisted.")
                return path, case.expected_mkwrite
            finally:
                mm.close()
        finally:
            os.close(fd)

    raise ValueError(f"unknown mmap case: {case.name}")


def parse_target_spec(text: str) -> TargetSpec:
    if "=" not in text:
        raise ValueError(f"invalid target '{text}', expected LABEL=DIR")
    label, directory = text.split("=", 1)
    label = label.strip()
    directory = directory.strip()
    if not label or not directory:
        raise ValueError(f"invalid target '{text}', expected LABEL=DIR")
    return TargetSpec(label=label, directory=directory)


def build_pattern_config_from_args(args: argparse.Namespace) -> PatternConfig:
    return PatternConfig(
        mode=args.pattern_mode,
        token=args.token.encode("utf-8"),
        seed=args.seed,
        chunk_size=args.chunk,
        pattern_gen=args.pattern_gen,
        readback=args.readback,
    )


def add_common_file_arg(parser: argparse.ArgumentParser, default_keep: bool) -> None:
    parser.add_argument("-f", "--file", default="/tmp/rw_test.bin", help="target file")
    if default_keep:
        parser.add_argument(
            "-k",
            "--keep-file",
            action="store_true",
            default=True,
            help="keep the target file [default on for scenario runners]",
        )
        parser.add_argument(
            "--cleanup-file",
            action="store_false",
            dest="keep_file",
            help="delete file created by this run when possible",
        )
    else:
        parser.add_argument("-k", "--keep-file", action="store_true", help="do not delete file created by this run")


def add_fsync_args(parser: argparse.ArgumentParser, default_on: bool = True) -> None:
    parser.add_argument(
        "--fsync",
        action="store_true",
        default=default_on,
        help="call fsync after write",
    )
    parser.add_argument(
        "--no-fsync",
        action="store_false",
        dest="fsync",
        help="disable fsync",
    )


def add_verify_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verify", action="store_true", help="(write) do verify after write")
    parser.add_argument(
        "--verify-mode",
        choices=["cache", "disk"],
        default="cache",
        help="verify readback mode: cache=normal read; disk=drop_caches then read",
    )


def add_pattern_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pattern-mode",
        choices=["repeat", "filepos", "counter"],
        default="repeat",
        help="pattern mode: repeat(region-local), filepos(file-offset anchored), counter(seed+pos)",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN,
        help="pattern token used by repeat/filepos modes",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for counter mode")
    parser.add_argument(
        "-pg",
        "--pattern-gen",
        choices=["stream", "direct"],
        default="stream",
        help="expected pattern generation: stream(const mem) or direct(alloc full bytes)",
    )
    parser.add_argument(
        "--readback",
        choices=["stream", "direct"],
        default="stream",
        help="verify readback: stream(pread chunk) or direct(pread full region)",
    )
    parser.add_argument("--chunk", type=str, default="1m", help="chunk size for streaming (supports b/k/m/g)")


def add_dump_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-dp", "--dump", action="store_true", help="dump aligned blocks / read hex")
    parser.add_argument("-bs", "--block-size", type=str, default="4k", help="dump block size")
    parser.add_argument("--dump-max", type=str, default="256k", help="max dump bytes")


def add_case_shape_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--baseline-kind", choices=["existing_a", "hole"], required=True)
    parser.add_argument("--baseline-len", required=True, help="baseline logical size")
    parser.add_argument("--write-style", choices=["overwrite", "append"], required=True)
    parser.add_argument("--offset", required=True, help="write offset for overwrite")
    parser.add_argument("--size", required=True, help="write size")
    parser.add_argument("--read-before-write", action="store_true", help="perform pre-read before write")
    parser.add_argument(
        "--read-before-write-passes",
        type=int,
        default=1,
        help="number of read passes for read-before-write mode",
    )
    parser.add_argument(
        "--drop-after-prepare",
        action="store_true",
        default=True,
        help="drop caches after baseline prepare [default on]",
    )
    parser.add_argument(
        "--no-drop-after-prepare",
        action="store_false",
        dest="drop_after_prepare",
        help="disable drop_caches after baseline prepare",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Buffered I/O and matrix-style rw test harness.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_p = subparsers.add_parser("read", aliases=["r"], help="read region")
    read_p.add_argument("offset", help="offset (supports b/k/m/g)")
    read_p.add_argument("size", help="size (supports b/k/m/g)")
    add_common_file_arg(read_p, default_keep=False)
    add_dump_args(read_p)

    write_p = subparsers.add_parser("write", aliases=["w"], help="write region")
    write_p.add_argument("offset", help="offset (supports b/k/m/g)")
    write_p.add_argument("size", help="size (supports b/k/m/g)")
    add_common_file_arg(write_p, default_keep=False)
    add_fsync_args(write_p, default_on=True)
    add_verify_args(write_p)
    add_pattern_args(write_p)
    add_dump_args(write_p)

    verify_p = subparsers.add_parser("verify", aliases=["v"], help="write and then verify region")
    verify_p.add_argument("offset", help="offset (supports b/k/m/g)")
    verify_p.add_argument("size", help="size (supports b/k/m/g)")
    add_common_file_arg(verify_p, default_keep=False)
    add_fsync_args(verify_p, default_on=True)
    add_verify_args(verify_p)
    add_pattern_args(verify_p)
    add_dump_args(verify_p)

    case_p = subparsers.add_parser("case", help="run one structured scenario case")
    case_p.add_argument("--name", default="ad_hoc_case", help="case name")
    add_common_file_arg(case_p, default_keep=True)
    add_case_shape_args(case_p)
    add_fsync_args(case_p, default_on=True)
    case_p.add_argument(
        "--verify-mode",
        choices=["cache", "disk"],
        default="disk",
        help="full-file verify mode",
    )
    add_pattern_args(case_p)
    add_dump_args(case_p)

    matrix_p = subparsers.add_parser("matrix", help="run builtin case matrix")
    matrix_p.add_argument(
        "--target",
        action="append",
        required=True,
        help="target as LABEL=DIR, repeatable",
    )
    matrix_p.add_argument(
        "--baseline-kind",
        action="append",
        choices=["existing_a", "hole"],
        default=[],
        help="filter matrix by baseline kind",
    )
    matrix_p.add_argument(
        "--write-style",
        action="append",
        choices=["overwrite", "append"],
        default=[],
        help="filter matrix by write style",
    )
    matrix_p.add_argument(
        "--case-filter",
        action="append",
        default=[],
        help="substring filter for builtin case names",
    )
    matrix_p.add_argument(
        "--include-read-then-write",
        action="store_true",
        default=True,
        help="include read-then-write variants [default on]",
    )
    matrix_p.add_argument(
        "--no-read-then-write",
        action="store_false",
        dest="include_read_then_write",
        help="disable read-then-write variants",
    )
    matrix_p.add_argument("--loops", type=int, default=1, help="repeat each selected case N times")
    matrix_p.add_argument(
        "--read-before-write-passes",
        type=int,
        default=1,
        help="number of read passes for read-then-write cases",
    )
    matrix_p.add_argument(
        "--verify-mode",
        choices=["cache", "disk"],
        default="disk",
        help="full-file verify mode",
    )
    matrix_p.add_argument(
        "--drop-after-prepare",
        action="store_true",
        default=True,
        help="drop caches after baseline prepare [default on]",
    )
    matrix_p.add_argument(
        "--no-drop-after-prepare",
        action="store_false",
        dest="drop_after_prepare",
        help="disable drop_caches after baseline prepare",
    )
    add_fsync_args(matrix_p, default_on=True)
    add_pattern_args(matrix_p)
    add_dump_args(matrix_p)

    mmap_case_p = subparsers.add_parser("mmap-case", help="run one builtin mmap scenario")
    mmap_case_p.add_argument("--name", required=True, help="builtin mmap case name")
    add_common_file_arg(mmap_case_p, default_keep=True)
    mmap_case_p.add_argument("--drop-caches", action="store_true", help="drop caches before case if supported")
    mmap_case_p.add_argument("--do-readahead", action="store_true", help="deprecated; kept for compatibility")
    mmap_case_p.add_argument("--pdb", action="store_true", help="break into pdb before mmap write when supported")

    mmap_matrix_p = subparsers.add_parser("mmap-matrix", help="run builtin mmap case matrix")
    mmap_matrix_p.add_argument(
        "--target",
        action="append",
        required=True,
        help="target as LABEL=DIR, repeatable",
    )
    mmap_matrix_p.add_argument(
        "--case-filter",
        action="append",
        default=[],
        help="substring filter for builtin mmap case names",
    )
    mmap_matrix_p.add_argument(
        "--include-wp-subpage",
        action="store_true",
        default=True,
        help="include wp_subpage_read_then_write [default on]",
    )
    mmap_matrix_p.add_argument(
        "--no-wp-subpage",
        action="store_false",
        dest="include_wp_subpage",
        help="exclude wp_subpage_read_then_write",
    )
    mmap_matrix_p.add_argument("--loops", type=int, default=1, help="repeat each selected case N times")
    mmap_matrix_p.add_argument("--drop-caches", action="store_true", help="force drop_caches before every case")
    mmap_matrix_p.add_argument("--do-readahead", action="store_true", help="deprecated; kept for compatibility")
    mmap_matrix_p.add_argument("--pdb", action="store_true", help="break into pdb before mmap write when supported")

    return parser


def normalize_size_args(args: argparse.Namespace, names: Iterable[str]) -> None:
    for name in names:
        value = getattr(args, name, None)
        if value is None:
            continue
        setattr(args, name, parse_bytes(value))


def do_verify(args: argparse.Namespace) -> bool:
    if not os.path.exists(args.file):
        print(f"[FAIL] file not exist: {args.file}")
        return False

    config = build_pattern_config_from_args(args)
    expected_direct = None
    expected_iter = None
    if config.pattern_gen == "direct":
        expected_direct, _ = build_pattern_source(args.offset, args.size, config)
    else:
        expected_iter = iter_expected_chunks(args.offset, args.size, config)

    if args.dump and args.verify_mode != "disk":
        dump_aligned_blocks(
            args.file,
            args.offset,
            args.size,
            block_size=args.block_size,
            max_dump_bytes=args.dump_max,
            title=f"BEFORE verify read ({args.verify_mode})",
        )

    if args.verify_mode == "disk":
        if args.dump:
            dump_aligned_blocks(
                args.file,
                args.offset,
                args.size,
                block_size=args.block_size,
                max_dump_bytes=args.dump_max,
                title="BEFORE drop_caches (disk verify)",
            )

        print("[verify] disk-mode: os.sync + drop_caches(3) + reopen")
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

    fd = None
    try:
        fd = open_fd_for_read(args.file)
        if expected_direct is not None:
            return verify_stream(fd, args.offset, args.size, expected_direct, None, args.chunk, config.readback)
        expected_iter2 = iter_expected_chunks(args.offset, args.size, config)
        return verify_stream(fd, args.offset, args.size, None, expected_iter2, args.chunk, config.readback)
    except Exception as exc:
        print(f"[FAIL] verify error: {exc}")
        return False
    finally:
        if fd is not None:
            os.close(fd)


def op_write(args: argparse.Namespace) -> bool:
    preexisted = ensure_file_exists(args.file)
    args._preexisted = preexisted
    config = build_pattern_config_from_args(args)

    ok = do_write_region(
        args.file,
        args.offset,
        args.size,
        config,
        fsync_after=args.fsync,
        dump=args.dump,
        block_size=args.block_size,
        dump_max=args.dump_max,
    )
    if not ok or not args.verify:
        return ok
    return do_verify(args)


def op_verify_write(args: argparse.Namespace) -> bool:
    args.verify = True
    return op_write(args)


def op_read(args: argparse.Namespace) -> bool:
    if not os.path.exists(args.file):
        print(f"[FAIL] file not exist: {args.file}")
        return False

    if args.size <= 0:
        print("[read] size=0")
        return True

    fd = None
    try:
        fd = open_fd_for_read(args.file)
        data = os.pread(fd, args.size, args.offset)
        print(f"[read] got {len(data)} bytes")
        if args.dump:
            print(hex_dump(data[:min(len(data), args.dump_max)], base=args.offset))
        return True
    except Exception as exc:
        print(f"[FAIL] read error: {exc}")
        return False
    finally:
        if fd is not None:
            os.close(fd)


def op_case(args: argparse.Namespace) -> bool:
    case = MatrixCase(
        name=args.name,
        baseline_kind=args.baseline_kind,
        baseline_len=args.baseline_len,
        write_style=args.write_style,
        offset=args.offset,
        size=args.size,
        read_before_write=args.read_before_write,
    )
    config = build_pattern_config_from_args(args)
    pseudo_target = TargetSpec(label="case", directory=os.path.dirname(args.file) or ".")
    target = TargetSpec(label=pseudo_target.label, directory=pseudo_target.directory)
    path = args.file

    print(f"[INFO] run ad-hoc case file={path}")
    if not prepare_baseline(
        path,
        case.baseline_kind,
        case.baseline_len,
        chunk_size=config.chunk_size,
        drop_after_prepare=args.drop_after_prepare,
    ):
        return False

    write_offset = resolve_write_offset(case)
    expected_len = resolve_expected_len(case, write_offset)

    if case.read_before_write:
        read_off, read_len = warmup_range_for_case(case, write_offset)
        total = read_region_discard(path, read_off, read_len, config.chunk_size, args.read_before_write_passes)
        print(
            "[warmup] read-before-write done "
            f"offset={read_off} size={read_len} passes={args.read_before_write_passes} total={total}"
        )

    if not do_write_region(
        path,
        write_offset,
        case.size,
        config,
        fsync_after=args.fsync,
        dump=args.dump,
        block_size=args.block_size,
        dump_max=args.dump_max,
    ):
        return False

    return verify_full_overlay(
        path,
        case.baseline_kind,
        case.baseline_len,
        write_offset,
        case.size,
        expected_len,
        config,
        cold_read=(args.verify_mode == "disk"),
    )


def op_matrix(args: argparse.Namespace) -> bool:
    try:
        targets = [parse_target_spec(text) for text in args.target]
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return False

    for target in targets:
        if not os.path.isdir(target.directory):
            print(f"[FAIL] target directory not found: {target.directory}")
            return False

    config = build_pattern_config_from_args(args)
    selected_cases = filter_matrix_cases(
        build_builtin_matrix_cases(include_read_then_write=args.include_read_then_write),
        args.baseline_kind,
        args.write_style,
        args.case_filter,
        args.include_read_then_write,
    )
    if not selected_cases:
        print("[FAIL] no matrix cases selected")
        return False

    print(f"[INFO] targets={','.join(f'{t.label}={t.directory}' for t in targets)}")
    print(f"[INFO] selected_cases={len(selected_cases)} loops={args.loops}")
    print(
        "[INFO] "
        f"pattern_mode={args.pattern_mode} token={args.token} seed={args.seed} "
        f"chunk={args.chunk} pattern_gen={args.pattern_gen} readback={args.readback}"
    )

    failures = []
    run_count = 0
    for target in targets:
        for case in selected_cases:
            for loop_idx in range(args.loops):
                run_count += 1
                if args.loops > 1:
                    print(f"[LOOP] {loop_idx + 1}/{args.loops} for {case.run_tag(target.label)}")
                ok = run_matrix_case(
                    case,
                    target,
                    config,
                    verify_mode=args.verify_mode,
                    fsync_after=args.fsync,
                    read_before_write_passes=args.read_before_write_passes,
                    drop_after_prepare=args.drop_after_prepare,
                    dump=args.dump,
                    block_size=args.block_size,
                    dump_max=args.dump_max,
                )
                if not ok:
                    failures.append(case.run_tag(target.label))

    print()
    print("==================== MATRIX SUMMARY ====================")
    print(f"runs={run_count} failures={len(failures)}")
    if failures:
        for item in failures:
            print(f"FAIL  {item}")
        print("========================================================")
        return False

    for target in targets:
        print(f"OK    target={target.label} dir={target.directory}")
    print("========================================================")
    return True


def op_mmap_case(args: argparse.Namespace) -> bool:
    try:
        case = find_builtin_mmap_case(args.name, include_wp_subpage=True)
    except KeyError:
        print(f"[FAIL] unknown mmap case: {args.name}")
        return False

    path = args.file
    print(f"[INFO] run mmap case name={case.name} file={path}")
    preexisted = os.path.exists(path)
    args._preexisted = preexisted
    try:
        run_builtin_mmap_case(
            case,
            path,
            drop_caches_override=args.drop_caches if args.drop_caches else None,
            do_readahead=args.do_readahead,
            pdb_before_write=args.pdb,
        )
        return True
    except Exception as exc:
        print(f"[FAIL] mmap case error: {exc}")
        return False


def op_mmap_matrix(args: argparse.Namespace) -> bool:
    try:
        targets = [parse_target_spec(text) for text in args.target]
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return False

    for target in targets:
        if not os.path.isdir(target.directory):
            print(f"[FAIL] target directory not found: {target.directory}")
            return False

    selected_cases = filter_builtin_mmap_cases(
        build_builtin_mmap_cases(include_wp_subpage=args.include_wp_subpage),
        args.case_filter,
    )
    if not selected_cases:
        print("[FAIL] no mmap cases selected")
        return False

    print(f"[INFO] targets={','.join(f'{t.label}={t.directory}' for t in targets)}")
    print(f"[INFO] selected_mmap_cases={len(selected_cases)} loops={args.loops}")

    failures = []
    run_count = 0
    for target in targets:
        for case in selected_cases:
            path = os.path.join(target.directory, f"{target.label}_{case.file_name}")
            for loop_idx in range(args.loops):
                run_count += 1
                print()
                print("================================================================")
                print(f"[RUN] mmap/{target.label}/{case.name}")
                print(f"      file={path}")
                if args.loops > 1:
                    print(f"      loop={loop_idx + 1}/{args.loops}")
                print("================================================================")
                try:
                    run_builtin_mmap_case(
                        case,
                        path,
                        drop_caches_override=args.drop_caches if args.drop_caches else None,
                        do_readahead=args.do_readahead,
                        pdb_before_write=args.pdb,
                    )
                    print(f"[OK ] mmap/{target.label}/{case.name}")
                except Exception as exc:
                    tag = f"mmap/{target.label}/{case.name}"
                    print(f"[FAIL] {tag}: {exc}")
                    failures.append(tag)

    print()
    print("==================== MMAP MATRIX SUMMARY ====================")
    print(f"runs={run_count} failures={len(failures)}")
    if failures:
        for item in failures:
            print(f"FAIL  {item}")
        print("============================================================")
        return False

    for target in targets:
        print(f"OK    target={target.label} dir={target.directory}")
    print("============================================================")
    return True


def cleanup_created_file(args: argparse.Namespace) -> None:
    if getattr(args, "keep_file", False):
        return

    preexisted = getattr(args, "_preexisted", os.path.exists(args.file))
    if (not preexisted) and os.path.exists(args.file):
        try:
            os.remove(args.file)
            print(f"[cleanup] deleted created file: {args.file}")
        except Exception as exc:
            print(f"[cleanup] failed to delete: {exc}")


def normalize_args(args: argparse.Namespace) -> None:
    normalize_size_args(args, ("chunk", "block_size", "dump_max"))

    if args.command in ("read", "r", "write", "w", "verify", "v"):
        normalize_size_args(args, ("offset", "size"))
    elif args.command == "case":
        normalize_size_args(args, ("baseline_len", "offset", "size"))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        normalize_args(args)
    except ValueError as exc:
        print(f"Error parsing args: {exc}", file=sys.stderr)
        parser.print_usage()
        return 2

    if getattr(args, "offset", 0) is not None and getattr(args, "offset", 0) < 0:
        print("[FAIL] offset cannot be negative")
        return 2
    if getattr(args, "size", 0) is not None and getattr(args, "size", 0) < 0:
        print("[FAIL] size cannot be negative")
        return 2
    if getattr(args, "baseline_len", 0) is not None and getattr(args, "baseline_len", 0) < 0:
        print("[FAIL] baseline_len cannot be negative")
        return 2

    command = args.command
    ok = False
    try:
        if command in ("read", "r"):
            ok = op_read(args)
        elif command in ("write", "w"):
            ok = op_write(args)
        elif command in ("verify", "v"):
            ok = op_verify_write(args)
        elif command == "case":
            ok = op_case(args)
        elif command == "matrix":
            ok = op_matrix(args)
        elif command == "mmap-case":
            ok = op_mmap_case(args)
        elif command == "mmap-matrix":
            ok = op_mmap_matrix(args)
        else:
            raise ValueError(f"unknown command {command}")
    finally:
        if command in ("read", "r", "write", "w", "verify", "v", "mmap-case"):
            cleanup_created_file(args)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())