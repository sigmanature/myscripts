#!/usr/bin/env python3
import argparse
import mmap
import os
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from typing import Tuple

TRACEFS = "/sys/kernel/tracing"
EVENT_ENABLE = f"{TRACEFS}/events/f2fs/f2fs_vm_page_mkwrite/enable"
TRACE_FILE = f"{TRACEFS}/trace"
TRACE_MARKER = f"{TRACEFS}/trace_marker"

PAGE = 4096
FOLIO_16K = 16 * 1024

def is_root() -> bool:
    return os.geteuid() == 0

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def write_text(path: str, s: str) -> None:
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
        f.write(s)

def append_marker(msg: str) -> None:
    if os.path.exists(TRACE_MARKER):
        write_text(TRACE_MARKER, msg + "\n")

def enable_tracepoint(enable: bool) -> None:
    if not os.path.exists(EVENT_ENABLE):
        raise RuntimeError(f"tracepoint not found: {EVENT_ENABLE} (check your kernel config / trace events)")
    write_text(EVENT_ENABLE, "1\n" if enable else "0\n")

def clear_trace() -> None:
    write_text(TRACE_FILE, "")

def drop_caches() -> None:
    # Stronger "read from disk" validation: flush + drop page cache.
    # Needs root.
    os.sync()
    write_text("/proc/sys/vm/drop_caches", "3\n")

def fill_file(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)

def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def expected_pattern(size: int) -> bytearray:
    # Deterministic baseline: byte i = i % 251 (avoid obvious repetition like 256)
    out = bytearray(size)
    for i in range(size):
        out[i] = i % 251
    return out

def mm_write(path: str, length: int, writes: Tuple[Tuple[int, bytes], ...]) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        mm = mmap.mmap(fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            for off, bs in writes:
                mm[off:off+len(bs)] = bs
            mm.flush()  # msync
        finally:
            mm.close()
        os.fsync(fd)
    finally:
        os.close(fd)

def count_events_between_markers(trace: str, start: str, end: str) -> int:
    lines = trace.splitlines()
    s_idx = None
    e_idx = None
    for i, ln in enumerate(lines):
        if start in ln:
            s_idx = i
        if end in ln and s_idx is not None and i > s_idx:
            e_idx = i
            break
    if s_idx is None or e_idx is None:
        # Fallback: count all events if markers missing
        return sum(1 for ln in lines if "f2fs_vm_page_mkwrite" in ln)

    seg = lines[s_idx:e_idx]
    return sum(1 for ln in seg if "f2fs_vm_page_mkwrite" in ln)

@dataclass
class TestResult:
    name: str
    ok: bool
    detail: str

def assert_equal(a: bytes, b: bytes, context: str) -> None:
    if a != b:
        # show first mismatch
        m = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), None)
        raise AssertionError(f"{context}: mismatch at offset {m}, got={a[m]:02x}, expect={b[m]:02x}")

def run_test_with_trace(name: str, fn) -> Tuple[TestResult, int]:
    start = f"=== {name} START ==="
    end = f"=== {name} END ==="

    append_marker(start)
    fn()
    append_marker(end)

    tr = read_text(TRACE_FILE)
    ev = count_events_between_markers(tr, start, end)
    return TestResult(name=name, ok=True, detail="ok"), ev

def test1_single_page(mount_dir: str) -> Tuple[str, int]:
    path = os.path.join(mount_dir, "t1_single_page.bin")
    size = 64 * 1024
    base = expected_pattern(size)
    fill_file(path, bytes(base))

    # write one byte in one 4K page
    off = 123
    base[off] = 0x5A
    mm_write(path, size, ((off, b"\x5A"),))

    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t1 disk content")
    return path, 1  # expected mkwrite count = 1 distinct page

def test2_four_pages_same_16k_folio(mount_dir: str) -> Tuple[str, int]:
    path = os.path.join(mount_dir, "t2_4pages_samefolio.bin")
    size = 64 * 1024
    base = expected_pattern(size)
    fill_file(path, bytes(base))

    # touch 4 different 4K pages inside first 16K (offsets: 0, 4096, 8192, 12288)
    writes = []
    for i, off in enumerate((0, PAGE, 2*PAGE, 3*PAGE)):
        base[off] = (0xA0 + i) & 0xFF
        writes.append((off, bytes([base[off]])))

    mm_write(path, size, tuple(writes))
    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t2 disk content")
    return path, 4  # 4 distinct 4K pages => 4 write faults (first write per page)

def test3_cross_16k_folio_boundary(mount_dir: str) -> Tuple[str, int]:
    path = os.path.join(mount_dir, "t3_cross_folio.bin")
    size = 128 * 1024
    base = expected_pattern(size)
    fill_file(path, bytes(base))

    # write 16 bytes starting at 16380, crossing 16K boundary (16384)
    off = FOLIO_16K - 4
    patch = bytes([0xEE] * 16)
    base[off:off+16] = patch

    mm_write(path, size, ((off, patch),))
    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t3 disk content")
    return path, 2  # crosses into next 4K page => at least 2 pages: (16380..16383) and (16384..)

def test4_sigbus_write_past_eof(mount_dir: str) -> Tuple[str, int]:
    # Run in subprocess so SIGBUS doesn't kill main runner.
    path = os.path.join(mount_dir, "t4_sigbus.bin")
    size = 4096
    fill_file(path, b"\x11" * size)

    helper = textwrap.dedent(f"""
        import mmap, os, sys
        path = {path!r}
        fd = os.open(path, os.O_RDWR)
        try:
            mm = mmap.mmap(fd, {size + 4096}, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
            # Write exactly at EOF -> should SIGBUS (pos == i_size)
            mm[{size}] = 0x22
            mm.flush()
            mm.close()
            os.fsync(fd)
        finally:
            os.close(fd)
        sys.exit(0)
    """).strip()

    cp = subprocess.run([sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Expect killed by SIGBUS
    if cp.returncode != -signal.SIGBUS:
        raise AssertionError(f"t4 expected SIGBUS (returncode {-signal.SIGBUS}), got {cp.returncode}, stderr={cp.stderr!r}")
    return path, 1  # it will fault that page once, then SIGBUS

def test5_tail_zeroing_off_in_folio(mount_dir: str) -> Tuple[str, int]:
    """
    Strong leak test:
    1) create 8192 bytes of 0xCC
    2) truncate down to 5000 (EOF in 2nd page, within first 16K folio)
    3) mmap-write last valid byte (offset 4999) OR trigger fault on page 1 by writing at 4096
    4) flush+fsync
    5) extend back to 8192
    6) read [5000..8191] must be zeros (otherwise tail wasn't zeroed and old 0xCC leaked)
    """
    path = os.path.join(mount_dir, "t5_tail_zeroing.bin")
    fill_file(path, b"\xCC" * 8192)

    # shrink to 5000
    fd = os.open(path, os.O_RDWR)
    try:
        os.ftruncate(fd, 5000)
        os.fsync(fd)
    finally:
        os.close(fd)

    # We want to ensure the fault hits the 2nd 4K page (pos=4096) so off_in_folio matters.
    # Writing at offset 4096 is inside i_size (5000), safe.
    fd = os.open(path, os.O_RDWR)
    try:
        mm = mmap.mmap(fd, 8192, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        mm[4096] = 0xAB  # triggers mkwrite on page index 1
        mm.flush()
        mm.close()
        os.fsync(fd)
    finally:
        os.close(fd)

    # extend back to 8192
    fd = os.open(path, os.O_RDWR)
    try:
        os.ftruncate(fd, 8192)
        os.fsync(fd)
    finally:
        os.close(fd)

    drop_caches()
    got = read_file(path)
    tail = got[5000:8192]
    if tail != b"\x00" * len(tail):
        # show first non-zero
        nz = next((i for i, b in enumerate(tail) if b != 0), None)
        raise AssertionError(f"t5 tail not zero at +{nz} (abs off {5000+nz}), val=0x{tail[nz]:02x}")
    return path, 1  # only touched one 4K page with first write

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="f2fs mount directory, e.g. /mnt/f2fs")
    ap.add_argument("--no-trace", action="store_true", help="run without tracefs verification")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"ERROR: not a directory: {args.dir}", file=sys.stderr)
        sys.exit(2)

    if not is_root():
        print("ERROR: run as root (needed for tracefs + drop_caches)", file=sys.stderr)
        sys.exit(2)

    if not args.no_trace:
        enable_tracepoint(True)
        clear_trace()

    tests = [
        ("t1_single_page", lambda: test1_single_page(args.dir)),
        ("t2_4pages_samefolio", lambda: test2_four_pages_same_16k_folio(args.dir)),
        ("t3_cross_folio", lambda: test3_cross_16k_folio_boundary(args.dir)),
        ("t4_sigbus_past_eof", lambda: test4_sigbus_write_past_eof(args.dir)),
        ("t5_tail_zeroing_off_in_folio", lambda: test5_tail_zeroing_off_in_folio(args.dir)),
    ]

    results = []
    for name, tf in tests:
        if not args.no_trace:
            def wrapped():
                path, exp = tf()
                # stash expectation on closure by attaching attributes
                wrapped.path = path
                wrapped.exp = exp

            # run with markers + count trace events
            append_marker(f"=== {name} START ===")
            wrapped()
            append_marker(f"=== {name} END ===")

            tr = read_text(TRACE_FILE)
            ev = count_events_between_markers(tr, f"=== {name} START ===", f"=== {name} END ===")
            path = getattr(wrapped, "path", "?")
            exp = getattr(wrapped, "exp", None)

            if exp is not None and ev != exp:
                results.append(TestResult(name, False, f"{path}: trace mkwrite events={ev}, expected={exp}"))
            else:
                results.append(TestResult(name, True, f"{path}: trace mkwrite events={ev}"))
        else:
            path, _exp = tf()
            results.append(TestResult(name, True, f"{path}: (no trace mode)"))

    if not args.no_trace:
        enable_tracepoint(False)

    # Print summary
    ok_all = True
    print("\n=== SUMMARY ===")
    for r in results:
        st = "PASS" if r.ok else "FAIL"
        print(f"{st}  {r.name}: {r.detail}")
        ok_all = ok_all and r.ok

    sys.exit(0 if ok_all else 1)

if __name__ == "__main__":
    main()
