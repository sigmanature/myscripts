#!/usr/bin/env python3
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import create_and_fill_file
from utils.patterns import PatternConfig, generate_expected_direct
from utils.sysutil import drop_caches

PAGE = 4096
ORDER = int(os.getenv("LF_ORDER", "2"))
FOLIO_SIZE = PAGE << ORDER
MNT = os.getenv("LF_MNT", "/mnt/f2fs")
SYSFS = os.getenv("LF_SYSFS", "/sys/fs/f2fs/vdb/large_folio_min_order")
OUTDIR = os.path.join(MNT, "lf_partial_read")


def cfg_repeat(token: bytes) -> PatternConfig:
    return PatternConfig(
        mode="repeat",
        token=token,
        seed=0,
        chunk_size=PAGE,
        pattern_gen="stream",
        readback="pread",
    )


BASE_CFG = cfg_repeat(b"A")
WRITE_CFG = cfg_repeat(b"B")


def set_largefolio_order(order: int) -> None:
    with open(SYSFS, "w", encoding="ascii") as f:
        f.write(str(order))
    with open(SYSFS, "r", encoding="ascii") as f:
        got = int(f.read().strip())
    if got != order:
        raise RuntimeError(f"large_folio_min_order mismatch: expect={order} got={got}")
    print(f"[setup] {SYSFS}={got}", flush=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def prepare_hole(path: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        os.ftruncate(fd, FOLIO_SIZE)
    finally:
        os.close(fd)
    drop_caches(3)


def prepare_existing(path: str) -> None:
    create_and_fill_file(path, FOLIO_SIZE, BASE_CFG)
    drop_caches(3)


def build_expected(kind: str, write_off: int) -> bytes:
    if kind == "hole":
        expected = bytearray(FOLIO_SIZE)
    else:
        expected = bytearray(generate_expected_direct(0, FOLIO_SIZE, BASE_CFG))
    expected[write_off:write_off + PAGE] = generate_expected_direct(write_off, PAGE, WRITE_CFG)
    return bytes(expected)


def run_case(name: str, kind: str, write_off: int) -> None:
    path = os.path.join(OUTDIR, f"{name}.bin")
    if kind == "hole":
        prepare_hole(path)
    elif kind == "existing":
        prepare_existing(path)
    else:
        raise RuntimeError(f"unknown kind: {kind}")

    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        wrote = os.pwrite(fd, generate_expected_direct(write_off, PAGE, WRITE_CFG), write_off)
        if wrote != PAGE:
            raise RuntimeError(f"{name}: short write {wrote}")
        data = os.pread(fd, FOLIO_SIZE, 0)
        if len(data) != FOLIO_SIZE:
            raise RuntimeError(f"{name}: short read {len(data)}")
        data2 = os.pread(fd, FOLIO_SIZE, 0)
        if len(data2) != FOLIO_SIZE:
            raise RuntimeError(f"{name}: short reread {len(data2)}")
    finally:
        os.close(fd)

    expected = build_expected(kind, write_off)
    if data != expected:
        raise RuntimeError(f"{name}: first read mismatch")
    if data2 != expected:
        raise RuntimeError(f"{name}: second read mismatch")
    print(f"[OK ] {name} kind={kind} write_off={write_off}", flush=True)


def main() -> None:
    ensure_dir(OUTDIR)
    set_largefolio_order(ORDER)
    cases = (
        ("hole_off0", "hole", 0),
        ("hole_off4k", "hole", PAGE),
        ("existing_off0", "existing", 0),
        ("existing_off4k", "existing", PAGE),
    )
    for name, kind, write_off in cases:
        run_case(name, kind, write_off)
    print("[OK ] all largefolio partial uptodate read cases passed", flush=True)


if __name__ == "__main__":
    main()
