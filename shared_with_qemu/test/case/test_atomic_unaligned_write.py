#!/usr/bin/env python3
"""
Atomic write case: unaligned writes (partial head and tail subpages).

Creates a file with pattern A, then atomically writes pattern B into a
region that is NOT 4K-aligned at either end:
  write region = [WRITE_OFF, WRITE_OFF + WRITE_LEN)
  WRITE_OFF is not a multiple of 4096
  WRITE_LEN does not end on a 4096 boundary

Verifies after commit:
  - bytes outside [WRITE_OFF, WRITE_OFF+WRITE_LEN) == pattern A (preserved)
  - bytes inside  [WRITE_OFF, WRITE_OFF+WRITE_LEN) == pattern B

This exercises the partial-subpage read-before-write path in
prepare_large_folio_atomic_write_begin (Stage B).
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
from utils.verify import OverlaySpec, verify_file_overlays

# === CONFIG ===
WORKDIR    = "/tmp/test_atomic_unaligned"
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT        = WORKDIR + "/mnt"
IMAGE_SIZE = 256 * 1024 * 1024
FILE_SIZE  = 4 * 1024 * 1024   # 4 MiB baseline

# Unaligned write region:
#   head partial: starts at 512 bytes into a 4K block
#   tail partial: ends at 1024 bytes into a 4K block
WRITE_OFF  = 4096 + 512          # 4608:  not 4K-aligned head
WRITE_LEN  = 4 * 4096 + 1024 - 512  # spans 4 full blocks + partial head/tail

CHUNK_SIZE = 256 * 1024
SEED_A     = 0x77
SEED_B     = 0x88


lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)
FILE_PATH = MNT + "/unaligned_test.bin"

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

    print("[prepare] write full file with pattern A", flush=True)
    create_and_fill_file(FILE_PATH, FILE_SIZE, BASELINE)
    os.sync()
    print(f"[prepare] file={FILE_SIZE} WRITE_OFF={WRITE_OFF} WRITE_LEN={WRITE_LEN}", flush=True)
    print("[prepare] done", flush=True)


def run() -> bool:
    print(f"[run] atomic unaligned write [{WRITE_OFF}, {WRITE_OFF+WRITE_LEN})", flush=True)
    fd = os.open(FILE_PATH, os.O_RDWR | os.O_CLOEXEC)
    try:
        atomic_start(fd)
        pwrite_pattern_config(fd, WRITE_OFF, WRITE_LEN, WRITE_PAT)
        fsync(fd)
        atomic_commit(fd)
    finally:
        os.close(fd)

    os.sync()
    drop_caches(3)

    print("[run] verify: outside range=A, inside range=B", flush=True)
    return verify_file_overlays(
        FILE_PATH,
        expected_size=FILE_SIZE,
        baseline=BASELINE,
        overlays=[
            OverlaySpec(
                offset=WRITE_OFF,
                length=WRITE_LEN,
                config=WRITE_PAT,
                overlay_off=WRITE_OFF,
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
        raise SystemExit("[FAIL] atomic unaligned write verify failed")
    print("[OK] test_atomic_unaligned_write passed", flush=True)


if __name__ == "__main__":
    main()
