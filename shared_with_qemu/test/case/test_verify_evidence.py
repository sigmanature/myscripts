#!/usr/bin/env python3
import json
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import pwrite_pattern_config
from utils.patterns import PatternConfig
from utils.verify import VerifyEvidenceConfig, verify_file_overlays


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_verify_evidence.")
TARGET = os.path.join(WORKDIR, "target.bin")
EVIDENCE_DIR = os.path.join(WORKDIR, "evidence")
FILE_SIZE = 64 * 1024
CHUNK_SIZE = 4096
SEED = 0x5A


def _cfg() -> PatternConfig:
    return PatternConfig(
        mode="mod251",
        token=b"",
        seed=SEED,
        chunk_size=CHUNK_SIZE,
        pattern_gen="stream",
        readback="pread",
    )


def prepare_bad_file() -> None:
    fd = os.open(TARGET, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        pwrite_pattern_config(fd, 0, FILE_SIZE, _cfg())
        os.pwrite(fd, b"\x00", 12345)
        os.fsync(fd)
    finally:
        os.close(fd)


def run() -> None:
    ok = verify_file_overlays(
        TARGET,
        expected_size=FILE_SIZE,
        baseline=_cfg(),
        overlays=(),
        chunk_size=CHUNK_SIZE,
        cold_read=False,
        evidence=VerifyEvidenceConfig(
            out_dir=EVIDENCE_DIR,
            label="verify_evidence_bad_byte",
            save_actual_file=True,
            save_expected_file=True,
            mismatch_window_bytes=4096,
        ),
    )
    if ok:
        raise RuntimeError("verify unexpectedly passed")

    case_dir = os.path.join(EVIDENCE_DIR, "verify_evidence_bad_byte")
    expected = [
        "actual.bin",
        "expected.bin",
        "actual.window.bin",
        "expected.window.bin",
        "mismatch.txt",
        "verify_meta.json",
    ]
    missing = [name for name in expected if not os.path.exists(os.path.join(case_dir, name))]
    if missing:
        raise RuntimeError(f"missing evidence files: {missing}")

    with open(os.path.join(case_dir, "verify_meta.json"), "r", encoding="utf-8") as fp:
        meta = json.load(fp)
    if meta.get("failure") != "mismatch":
        raise RuntimeError(f"unexpected failure kind: {meta.get('failure')}")
    if meta.get("mismatch_offset") != 12345:
        raise RuntimeError(f"unexpected mismatch offset: {meta.get('mismatch_offset')}")


def cleanup() -> None:
    shutil.rmtree(WORKDIR, ignore_errors=True)


def main() -> None:
    try:
        prepare_bad_file()
        run()
    finally:
        cleanup()
    print("[OK] test_verify_evidence passed", flush=True)


if __name__ == "__main__":
    main()
