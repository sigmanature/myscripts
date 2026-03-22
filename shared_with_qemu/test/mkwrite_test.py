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

def log(msg: str) -> None:
    print(msg, flush=True)

def dump_bytes(label: str, data: bytes, start: int, length: int = 16) -> None:
    end = min(len(data), start + length)
    chunk = data[start:end]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    log(f"{label}: off={start} len={len(chunk)} bytes=[{hexs}]")

def expected_pattern(size: int) -> bytearray:
    # Deterministic baseline: byte i = i % 251 (avoid obvious repetition like 256)
    out = bytearray(size)
    for i in range(size):
        out[i] = i % 251
    return out

def mm_write(path: str, length: int, writes: Tuple[Tuple[int, bytes], ...]) -> None:
    log(f"MM_WRITE begin path={path} len={length} inode={stat_inode(path)} writes={[(off, bs.hex()) for off, bs in writes]}")
    fd = os.open(path, os.O_RDWR)
    try:
        mm = mmap.mmap(fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            for off, bs in writes:
                log(f"MM_WRITE store off={off} size={len(bs)} data={bs.hex()}")
                mm[off:off+len(bs)] = bs
            mm.flush()  # msync
            log("MM_WRITE flush done")
        finally:
            mm.close()
            log("MM_WRITE close done")
        os.fsync(fd)
        log("MM_WRITE fsync done")
    finally:
        os.close(fd)
        log("MM_WRITE end")

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

def stat_inode(path: str) -> int:
    return os.stat(path).st_ino


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
    log(f"TEST t1 start path={path} inode={stat_inode(path)} size={size}")
    dump_bytes("TEST t1 base-before", base, 112)

    # write one byte in one 4K page
    off = 123
    base[off] = 0x5A
    dump_bytes("TEST t1 base-after", base, 112)
    mm_write(path, size, ((off, b"\x5A"),))

    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t1 disk content")
    return path, 1  # expected mkwrite count = 1 distinct page

def test2_four_pages_same_16k_folio(mount_dir: str) -> Tuple[str, int]:
    drop_caches()
    path = os.path.join(mount_dir, "t2_4pages_samefolio.bin")
    size = 64 * 1024
    base = expected_pattern(size)
    fill_file(path, bytes(base))
    log(f"TEST t2 start path={path} inode={stat_inode(path)} size={size}")
    dump_bytes("TEST t2 base-before@0", base, 0)
    dump_bytes("TEST t2 base-before@4096", base, PAGE)
    dump_bytes("TEST t2 base-before@8192", base, 2 * PAGE)
    dump_bytes("TEST t2 base-before@12288", base, 3 * PAGE)

    # touch 4 different 4K pages inside first 16K (offsets: 0, 4096, 8192, 12288)
    writes = []
    for i, off in enumerate((0, PAGE, 2*PAGE, 3*PAGE)):
        base[off] = (0xA0 + i) & 0xFF
        writes.append((off, bytes([base[off]])))
    log(f"TEST t2 writes={[(off, bs.hex()) for off, bs in writes]}")
    dump_bytes("TEST t2 base-after@0", base, 0)
    dump_bytes("TEST t2 base-after@4096", base, PAGE)
    dump_bytes("TEST t2 base-after@8192", base, 2 * PAGE)
    dump_bytes("TEST t2 base-after@12288", base, 3 * PAGE)

    mm_write(path, size, tuple(writes))
    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t2 disk content")
    return path, 4  # 4 distinct 4K pages => 4 write faults (first write per page)

def test3_cross_16k_folio_boundary(mount_dir: str) -> Tuple[str, int]:
    drop_caches()
    path = os.path.join(mount_dir, "t3_cross_folio.bin")
    size = 128 * 1024
    base = expected_pattern(size)
    fill_file(path, bytes(base))
    log(f"TEST t3 start path={path} inode={stat_inode(path)} size={size}")

    # write 16 bytes starting at 16380, crossing 16K boundary (16384)
    off = FOLIO_16K - 4
    patch = bytes([0xEE] * 16)
    dump_bytes("TEST t3 base-before", base, off, 24)
    base[off:off+16] = patch
    dump_bytes("TEST t3 base-after", base, off, 24)

    mm_write(path, size, ((off, patch),))
    drop_caches()
    got = read_file(path)
    assert_equal(got, bytes(base), "t3 disk content")
    return path, 2  # crosses into next 4K page => at least 2 pages: (16380..16383) and (16384..)

def test4_sigbus_write_past_eof(mount_dir: str) -> Tuple[str, int]:
    drop_caches()
    # Run in subprocess so SIGBUS doesn't kill main runner.
    path = os.path.join(mount_dir, "t4_sigbus.bin")
    size = 4096
    fill_file(path, b"\x11" * size)
    log(f"TEST t4 start path={path} inode={stat_inode(path)} size={size}")

    helper = textwrap.dedent(f"""
    import mmap, os, sys

    path = {path!r}
    orig = {size}
    mapsz = {size + 4096}

    fd = os.open(path, os.O_RDWR)
    try:
        # 1) extend so mmap(length) won't fail in python
        os.ftruncate(fd, mapsz)

        mm = mmap.mmap(fd, mapsz, flags=mmap.MAP_SHARED,
                      prot=mmap.PROT_READ | mmap.PROT_WRITE)

        # 2) shrink back so offset==orig becomes EOF
        os.ftruncate(fd, orig)

        # 3) write exactly at EOF -> should SIGBUS
        mm[orig] = 0x22

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
    log(f"TEST t5 start path={path} inode={stat_inode(path)} size=8192")

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
    dump_bytes("TEST t5 readback@4096", got, 4096, 32)
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
    ap.add_argument("--only", action="append", default=[],
                    help="run only selected test(s), e.g. --only t2_4pages_samefolio")
    ap.add_argument("--only-prefix", action="append", default=[],
                    help="run tests whose names start with the given prefix, e.g. --only-prefix t2")
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

    if args.only:
        only = set(args.only)
        tests = [(name, tf) for name, tf in tests if name in only]
        missing = sorted(only - {name for name, _ in tests})
        if missing:
            raise SystemExit(f"unknown --only test(s): {', '.join(missing)}")

    if args.only_prefix:
        prefixes = tuple(args.only_prefix)
        tests = [(name, tf) for name, tf in tests if name.startswith(prefixes)]
        if not tests:
            raise SystemExit(f"no tests matched --only-prefix: {', '.join(args.only_prefix)}")

    if args.only or args.only_prefix:
        log(f"RUNNER selected tests={','.join(name for name, _ in tests)}")

    results = []
    for name, tf in tests:
        log(f"RUNNER begin test={name}")
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
            ino = stat_inode(path) if path != "?" and os.path.exists(path) else -1

            if exp is not None and ev != exp:
                results.append(TestResult(name, False, f"{path} inode={ino}: trace mkwrite events={ev}, expected={exp}"))
            else:
                results.append(TestResult(name, True, f"{path} inode={ino}: trace mkwrite events={ev}"))
        else:
            path, _exp = tf()
            ino = stat_inode(path) if os.path.exists(path) else -1
            results.append(TestResult(name, True, f"{path} inode={ino}: (no trace mode)"))

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
