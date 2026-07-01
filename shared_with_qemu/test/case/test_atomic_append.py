#!/usr/bin/env python3
"""
Atomic write case: append to an existing file.

Creates a file with pattern A (FILE_SIZE bytes), then opens it atomic,
writes pattern B starting at FILE_SIZE (appending APPEND_SIZE bytes),
commits, and verifies:
  - [0, FILE_SIZE)         == pattern A  (untouched)
  - [FILE_SIZE, total)     == pattern B  (appended)
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import atomic_commit, atomic_start, create_and_fill_file, ensure_dir, fsync, pwrite_pattern_config
from utils.loop_mount import LoopMount
from utils.patterns import PatternConfig, render_pattern_bytes
from utils.sysutil import drop_caches
from utils.verify import OverlaySpec, verify_file_overlays

# === CONFIG ===
WORKDIR      = "/tmp/test_atomic_append"
IMAGE_PATH   = WORKDIR + "/f2fs.img"
MNT          = WORKDIR + "/mnt"
IMAGE_SIZE   = 256 * 1024 * 1024
FILE_SIZE    = 2 * 1024 * 1024   # 2 MiB  (baseline region)
APPEND_SIZE  = 2 * 1024 * 1024   # 2 MiB  (appended region)
CHUNK_SIZE   = 256 * 1024
SEED_A       = 0x11
SEED_B       = 0x22

lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)
FILE_PATH = MNT + "/append_test.bin"

BASELINE = PatternConfig(
    mode="mod251", token=b"", seed=SEED_A,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)
APPEND_PAT = PatternConfig(
    mode="mod251", token=b"", seed=SEED_B,
    chunk_size=CHUNK_SIZE, pattern_gen="mod251", readback="pread",
)


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)

    print("[prepare] write baseline file (pattern A)", flush=True)
    create_and_fill_file(FILE_PATH, FILE_SIZE, BASELINE)
    os.sync()
    print("[prepare] done", flush=True)


def run() -> bool:
    total = FILE_SIZE + APPEND_SIZE
    print(f"[run] atomic append {APPEND_SIZE} bytes at offset {FILE_SIZE}", flush=True)
    fd = os.open(FILE_PATH, os.O_RDWR | os.O_CLOEXEC)
    try:
        atomic_start(fd)
        pwrite_pattern_config(fd, FILE_SIZE, APPEND_SIZE, APPEND_PAT)
        fsync(fd)
        atomic_commit(fd)
    finally:
        os.close(fd)

    os.sync()
    drop_caches(3)

    print("[run] verify: [0,FILE_SIZE)=A  [FILE_SIZE,total)=B", flush=True)
    return verify_file_overlays(
        FILE_PATH,
        expected_size=total,
        baseline=BASELINE,
        overlays=[
            OverlaySpec(
                offset=FILE_SIZE,
                length=APPEND_SIZE,
                config=APPEND_PAT,
                overlay_off=FILE_SIZE,
            )
        ],
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
        raise SystemExit("[FAIL] atomic append verify failed")
    print("[OK] test_atomic_append passed", flush=True)


if __name__ == "__main__":
    main()
