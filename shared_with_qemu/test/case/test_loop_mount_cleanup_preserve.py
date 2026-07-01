#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.loop_mount import LoopMount


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_loop_mount_cleanup.")
IMAGE_PATH = os.path.join(WORKDIR, "f2fs.img")
MNT = os.path.join(WORKDIR, "mnt")


def prepare() -> None:
    os.makedirs(MNT, exist_ok=True)
    with open(IMAGE_PATH, "wb") as fp:
        fp.write(b"sample")


def run() -> None:
    lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)
    lm.cleanup(remove_image=False)
    if not os.path.exists(IMAGE_PATH):
        raise RuntimeError("cleanup(remove_image=False) removed image")

    lm.cleanup(remove_image=True)
    if os.path.exists(IMAGE_PATH):
        raise RuntimeError("cleanup(remove_image=True) preserved image")


def cleanup() -> None:
    shutil.rmtree(WORKDIR, ignore_errors=True)


def main() -> None:
    try:
        prepare()
        run()
    finally:
        cleanup()
    print("[OK] test_loop_mount_cleanup_preserve passed", flush=True)


if __name__ == "__main__":
    main()
