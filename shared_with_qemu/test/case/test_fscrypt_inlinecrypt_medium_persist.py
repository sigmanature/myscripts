#!/usr/bin/env python3
import os
import sys

# make "test/utils" importable when running from shared mount
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.fscrypt_inline import ensure_inline_fscrypt_dir
from utils.io import ensure_file_size, fsync, open_rw, pwrite_pattern_config
from utils.patterns import PatternConfig
from utils.verify import verify_file_overlays

# === CONFIG (edit here) ===
MOUNT_ROOT = "/mnt/f2fs"
ENC_DIR_NAME = "enc_test"
FSCRYPT_KEY = "/opt/test-secrets/fscrypt-ci.key"
PROTECTOR_NAME = "smoke-inlinecrypt"

FILE_NAME = "medium_persist_32m_repeat.bin"
FILE_SIZE = 32 * 1024 * 1024

CHUNK_SIZE = 256 * 1024
SEED = 12345

# Faster-than-mod251 pattern: repeating 4KiB token with offset-dependent phase.
TOKEN_4K = bytes(range(256)) * 16


def ensure_medium_file(path: str, size: int, baseline: PatternConfig) -> None:
    if os.path.exists(path):
        st = os.stat(path)
        print(f"[*] exists: {path} size={st.st_size}", flush=True)
        return

    print(f"[*] creating: {path} size={size}", flush=True)
    fd = open_rw(path, create=True)
    try:
        ensure_file_size(fd, size)
        wrote = pwrite_pattern_config(fd, 0, size, baseline)
        if wrote != size:
            raise RuntimeError(f"short write: wrote={wrote} expected={size}")
        fsync(fd)
    finally:
        os.close(fd)
    os.sync()
    print("[*] write+fsync+sync done", flush=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root (drop_caches requires root)")

    enc_root = ensure_inline_fscrypt_dir(
        mount_root=MOUNT_ROOT,
        enc_dir_name=ENC_DIR_NAME,
        key_path=FSCRYPT_KEY,
        protector_name=PROTECTOR_NAME,
    )

    baseline = PatternConfig(
        mode="repeat",
        token=TOKEN_4K,
        seed=SEED,
        chunk_size=CHUNK_SIZE,
        pattern_gen="repeat_4k",
        readback="pread",
    )

    target = os.path.join(enc_root, FILE_NAME)
    ensure_medium_file(target, FILE_SIZE, baseline)

    print("[*] cold verify: sync + drop_caches + reopen + compare", flush=True)
    ok = verify_file_overlays(
        target,
        expected_size=FILE_SIZE,
        baseline=baseline,
        overlays=(),
        chunk_size=CHUNK_SIZE,
        cold_read=True,
    )
    if not ok:
        raise SystemExit("verify failed")

    print("[OK] medium file verified", flush=True)


if __name__ == "__main__":
    main()
