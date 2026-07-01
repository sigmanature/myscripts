#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import ensure_dir
from utils.loop_mount import LoopMount
from utils.oatwriter_mmap import OatWriterStressConfig, run_oatwriter_mmap_stress


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_oatwriter_mmap_stress.")
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT = WORKDIR + "/mnt"
IMAGE_SIZE = _env_int("OATWRITER_IMAGE_MB", 512) * 1024 * 1024
GROUPS = _env_int("OATWRITER_GROUPS", 24)
FILES_PER_GROUP = _env_int("OATWRITER_FILES_PER_GROUP", 3)
DEX_BLOB_COUNT = _env_int("OATWRITER_DEX_BLOB_COUNT", 2)
DEX_BLOB_SIZE = _env_int("OATWRITER_DEX_BLOB_MB", 3) * 1024 * 1024
EXTRA_BUFFER_SIZE = _env_int("OATWRITER_EXTRA_BUFFER_KB", 1537) * 1024
PRESSURE_MB = _env_int("OATWRITER_PRESSURE_MB", 256)
SEED = _env_int("OATWRITER_SEED", 20260423)


lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)
    print(f"[prepare] mounted at {MNT}", flush=True)


def run() -> None:
    summary = run_oatwriter_mmap_stress(
        MNT,
        OatWriterStressConfig(
            groups=GROUPS,
            files_per_group=FILES_PER_GROUP,
            dex_blob_count=DEX_BLOB_COUNT,
            dex_blob_size=DEX_BLOB_SIZE,
            extra_buffer_size=EXTRA_BUFFER_SIZE,
            pressure_bytes=PRESSURE_MB * 1024 * 1024,
            seed=SEED,
            verify_drop_caches=True,
            keep_files=False,
        ),
    )
    print(
        f"[run] files={summary.files_written} verified={summary.files_verified} "
        f"bytes={summary.total_verified_bytes} peak_size={summary.peak_file_size}",
        flush=True,
    )


def cleanup() -> None:
    print("[cleanup]", flush=True)
    lm.cleanup(verbose=True)
    shutil.rmtree(WORKDIR, ignore_errors=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")
    try:
        prepare()
        run()
    finally:
        cleanup()
    print("[OK] test_oatwriter_mmap_stress passed", flush=True)


if __name__ == "__main__":
    main()
