#!/usr/bin/env python3
"""
Atomic write case: read-then-write (page cache warm).

Creates a file with pattern A, then does a full buffered read to warm
the page cache (folios are now in memory), then opens atomic and writes
pattern B. Verifies on-disk content is pattern B after commit.

The key scenario: write_begin must handle the case where the folio
already exists in page cache (uptodate) before the atomic write.
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import atomic_commit, atomic_start, create_and_fill_file, ensure_dir, fsync, pread_scan, pwrite_pattern_config
from utils.loop_mount import LoopMount
from utils.patterns import PatternConfig
from utils.sysutil import drop_caches
from utils.verify import verify_file_overlays

# === CONFIG ===
WORKDIR    = "/tmp/test_atomic_read_then_write"
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT        = WORKDIR + "/mnt"
IMAGE_SIZE = 256 * 1024 * 1024
FILE_SIZE  = 4 * 1024 * 1024
CHUNK_SIZE = 256 * 1024
SEED_A     = 0xCC
SEED_B     = 0xDD


lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)
FILE_PATH = MNT + "/read_then_write_test.bin"

BASELINE = PatternConfig(
    mode="mod251", token=b"", seed=SEED_A,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)
WRITE_PAT = PatternConfig(
    mode="mod251", token=b"", seed=SEED_B,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)

    print("[prepare] write file with pattern A", flush=True)
    create_and_fill_file(FILE_PATH, FILE_SIZE, BASELINE)
    os.sync()
    print("[prepare] done", flush=True)


def run() -> bool:
    # Step 1: warm page cache — read the whole file
    print("[run] warming page cache: full buffered read", flush=True)
    fd = os.open(FILE_PATH, os.O_RDONLY)
    try:
        total_read = pread_scan(fd, 0, FILE_SIZE, chunk=CHUNK_SIZE)
    finally:
        os.close(fd)
    print(f"[run] read {total_read} bytes into page cache", flush=True)

    # Step 2: atomic write over the warm folios
    print("[run] open atomic, overwrite with pattern B (cache is warm)", flush=True)
    fd = os.open(FILE_PATH, os.O_RDWR | os.O_CLOEXEC)
    try:
        atomic_start(fd)
        pwrite_pattern_config(fd, 0, FILE_SIZE, WRITE_PAT)
        fsync(fd)
        atomic_commit(fd)
    finally:
        os.close(fd)

    os.sync()
    drop_caches(3)

    print("[run] verify on-disk == pattern B", flush=True)
    return verify_file_overlays(
        FILE_PATH,
        expected_size=FILE_SIZE,
        baseline=WRITE_PAT,
        overlays=(),
        chunk_size=CHUNK_SIZE,
        cold_read=False,
    )


def cleanup() -> None:
    print("[cleanup]", flush=True)
    lm.cleanup(verbose=True)
    try:
        import shutil
        shutil.rmtree(WORKDIR, ignore_errors=True)
    except Exception:
        pass


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")
    try:
        prepare()
        ok = run()
    finally:
        cleanup()
    if not ok:
        raise SystemExit("[FAIL] atomic read-then-write verify failed")
    print("[OK] test_atomic_read_then_write passed", flush=True)


if __name__ == "__main__":
    main()
