import os
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Tuple

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


def hex_dump(data: bytes, base: int = 0, bytes_per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk).ljust(bytes_per_line * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


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
) -> bool:
    """Verify full file content on disk as baseline overridden by overlays."""
    if expected_size < 0:
        raise ValueError("expected_size must be >= 0")

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
        return False

    if st.st_size != expected_size:
        print(f"[FAIL] size mismatch: actual={st.st_size} expected={expected_size}")
        return False

    ovs = list(overlays)
    fd = os.open(path, os.O_RDONLY)
    try:
        pos = 0
        while pos < expected_size:
            n = min(chunk_size, expected_size - pos)
            got = os.pread(fd, n, pos)
            if len(got) != n:
                print(f"[FAIL] short read at {pos}: got {len(got)} expect {n}")
                return False

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

            if got != bytes(exp):
                # Find first mismatch to print a tight window.
                idx = next((i for i in range(n) if got[i] != exp[i]), 0)
                win_off = max(0, pos + idx - 64)
                win_end = min(expected_size, pos + idx + 64)
                got_win = os.pread(fd, win_end - win_off, win_off)
                exp_win = bytearray(render_pattern_bytes(win_off, win_end - win_off, baseline, overlay_off=0))
                for ov in ovs:
                    if ov.length <= 0:
                        continue
                    ov_end = ov.offset + ov.length
                    seg_start = max(win_off, ov.offset)
                    seg_end = min(win_end, ov_end)
                    if seg_start >= seg_end:
                        continue
                    rel = seg_start - win_off
                    seg_len = seg_end - seg_start
                    exp_win[rel:rel + seg_len] = render_pattern_bytes(
                        seg_start,
                        seg_len,
                        ov.config,
                        overlay_off=ov.overlay_off,
                    )
                print(f"[FAIL] mismatch around offset {win_off}")
                print("Expected slice:")
                print(hex_dump(bytes(exp_win), base=win_off))
                print("Actual slice:")
                print(hex_dump(got_win, base=win_off))
                return False

            pos += n

        print("[OK] disk verify pass")
        return True
    finally:
        os.close(fd)
