#!/usr/bin/env python3
"""
Atomic write case: overwrite an existing file.

Creates a file with pattern A, then opens it as atomic (O_ATOMIC),
writes pattern B over the full file, commits (close), and verifies
that on-disk content matches pattern B.
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import atomic_commit, atomic_start, create_and_fill_file, ensure_dir, fsync, pwrite_pattern_config
from utils.loop_mount import LoopMount
from utils.patterns import PatternConfig
from utils.sysutil import drop_caches
from utils.verify import verify_file_overlays

# === CONFIG ===
WORKDIR     = "/tmp/test_atomic_overwrite"
IMAGE_PATH  = WORKDIR + "/f2fs.img"
MNT         = WORKDIR + "/mnt"
IMAGE_SIZE  = 256 * 1024 * 1024
FILE_SIZE   = 4 * 1024 * 1024   # 4 MiB
CHUNK_SIZE  = 256 * 1024
SEED_A      = 0xAA
SEED_B      = 0xBB

lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)
FILE_PATH = MNT + "/overwrite_test.bin"

BASELINE = PatternConfig(
    mode="mod251", token=b"", seed=SEED_A,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)
OVERLAY = PatternConfig(
    mode="mod251", token=b"", seed=SEED_B,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)

    print("[prepare] write baseline (pattern A)", flush=True)
    create_and_fill_file(FILE_PATH, FILE_SIZE, BASELINE)
    os.sync()
    print("[prepare] done", flush=True)


def run() -> bool:
    print("[run] open atomic, overwrite with pattern B", flush=True)
    fd = os.open(FILE_PATH, os.O_RDWR | os.O_CLOEXEC)
    try:
        atomic_start(fd)
        pwrite_pattern_config(fd, 0, FILE_SIZE, OVERLAY)
        fsync(fd)
        atomic_commit(fd)
    finally:
        os.close(fd)

    os.sync()
    drop_caches(3)

    print("[run] verify on-disk content == pattern B", flush=True)
    return verify_file_overlays(
        FILE_PATH,
        expected_size=FILE_SIZE,
        baseline=OVERLAY,
        overlays=(),
        chunk_size=CHUNK_SIZE,
        cold_read=False,  # already dropped caches above
    )


def cleanup() -> None:
    print("[cleanup] umount + remove image", flush=True)
    lm.cleanup(verbose=True)
    try:
        import shutil
        shutil.rmtree(WORKDIR, ignore_errors=True)
    except Exception:
        pass
    print("[cleanup] done", flush=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")
    try:
        prepare()
        ok = run()
    finally:
        cleanup()
    if not ok:
        raise SystemExit("[FAIL] atomic overwrite verify failed")
    print("[OK] test_atomic_overwrite passed", flush=True)


if __name__ == "__main__":
    main()
