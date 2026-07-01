#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.artifact_pressure import ArtifactPressureConfig, run_inline_artifact_pressure
from utils.fscrypt_inline import ensure_inline_fscrypt_dir
from utils.io import ensure_dir
from utils.loop_mount import LoopMount


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


# === CONFIG ===
WORKDIR = os.getenv("INLINE_ARTIFACT_WORKDIR", tempfile.mkdtemp(prefix="test_inline_artifact."))
IMAGE_PATH = os.path.join(WORKDIR, "f2fs.img")
MNT = os.path.join(WORKDIR, "mnt")
IMAGE_SIZE = _env_int("INLINE_ARTIFACT_IMAGE_MB", 2048) * 1024 * 1024
MOUNT_OPTS = os.getenv("INLINE_ARTIFACT_MOUNT_OPTS", "inlinecrypt")

ENC_DIR_NAME = os.getenv("INLINE_ARTIFACT_ENC_DIR", "enc_test")
FSCRYPT_KEY = os.getenv("INLINE_ARTIFACT_KEY", "/opt/test-secrets/fscrypt-ci.key")
PROTECTOR_NAME = os.getenv("INLINE_ARTIFACT_PROTECTOR", "inline-artifact-pressure")

GROUPS = _env_int("INLINE_ARTIFACT_GROUPS", 200)
APP_COUNT = _env_int("INLINE_ARTIFACT_APP_COUNT", 96)
ARTIFACTS_PER_APP = _env_int("INLINE_ARTIFACTS_PER_APP", 3)
ARTIFACT_MIN_MB = _env_int("INLINE_ARTIFACT_MIN_MB", 2)
ARTIFACT_MAX_MB = _env_int("INLINE_ARTIFACT_MAX_MB", 24)
PREFILL_PERCENT = _env_int("INLINE_ARTIFACT_PREFILL_PERCENT", 80)
PREFILL_FILE_MB = _env_int("INLINE_ARTIFACT_PREFILL_FILE_MB", 16)
CHURN_FILES_PER_ROUND = _env_int("INLINE_ARTIFACT_CHURN_FILES", 32)
CHURN_FILE_KB = _env_int("INLINE_ARTIFACT_CHURN_FILE_KB", 512)
CHURN_KEEP = _env_float("INLINE_ARTIFACT_CHURN_KEEP", 0.35)
CHURN_INTERVAL = _env_float("INLINE_ARTIFACT_CHURN_INTERVAL", 0.02)
MEMORY_PRESSURE_MB = _env_int("INLINE_ARTIFACT_MEMORY_MB", 1024)
GC_INTERVAL = _env_float("INLINE_ARTIFACT_GC_INTERVAL", 0.3)
SYNC_EVERY = _env_int("INLINE_ARTIFACT_SYNC_EVERY", 10)
FSYNC_WORKERS = _env_int("INLINE_ARTIFACT_FSYNC_WORKERS", 8)
FSYNC_BATCH_WIDTH = _env_int("INLINE_ARTIFACT_FSYNC_BATCH_WIDTH", 16)
VERITY_RATIO_PERCENT = _env_int("INLINE_ARTIFACT_VERITY_RATIO", 100)
VERITY_MIN_FSYNC_PASSES = _env_int("INLINE_ARTIFACT_VERITY_MIN_FSYNC", 2)
VERIFY_EVERY = _env_int("INLINE_ARTIFACT_VERIFY_EVERY", 1)
SEED = _env_int("INLINE_ARTIFACT_SEED", 20260429)
SAVE_EXPECTED = _env_int("INLINE_ARTIFACT_SAVE_EXPECTED_FULL", 0) != 0
KEEP_SUCCESS_FILES = _env_int("INLINE_ARTIFACT_KEEP_SUCCESS_FILES", 1) != 0
KEEP_WORKDIR_ON_FAILURE = _env_int("INLINE_ARTIFACT_KEEP_WORKDIR_ON_FAILURE", 1) != 0

EVIDENCE_DIR = os.getenv("INLINE_ARTIFACT_EVIDENCE_DIR", os.path.join(WORKDIR, "failures"))

lm = LoopMount(
    image_path=IMAGE_PATH,
    mountpoint=MNT,
    mount_opts=MOUNT_OPTS,
    mkfs_features=("encrypt", "verity"),
)


def prepare() -> str:
    print("[prepare] setup inlinecrypt f2fs image", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)
    enc_root = ensure_inline_fscrypt_dir(
        mount_root=MNT,
        enc_dir_name=ENC_DIR_NAME,
        key_path=FSCRYPT_KEY,
        protector_name=PROTECTOR_NAME,
    )
    print(f"[prepare] enc_root={enc_root}", flush=True)
    return enc_root


def run(enc_root: str) -> None:
    summary = run_inline_artifact_pressure(
        enc_root,
        ArtifactPressureConfig(
            groups=GROUPS,
            app_count=APP_COUNT,
            artifacts_per_app=ARTIFACTS_PER_APP,
            artifact_min_bytes=ARTIFACT_MIN_MB * 1024 * 1024,
            artifact_max_bytes=ARTIFACT_MAX_MB * 1024 * 1024,
            prefill_percent=PREFILL_PERCENT,
            prefill_file_bytes=PREFILL_FILE_MB * 1024 * 1024,
            churn_files_per_round=CHURN_FILES_PER_ROUND,
            churn_file_bytes=CHURN_FILE_KB * 1024,
            churn_keep_fraction=CHURN_KEEP,
            churn_interval_s=CHURN_INTERVAL,
            memory_pressure_bytes=MEMORY_PRESSURE_MB * 1024 * 1024,
            gc_interval_s=GC_INTERVAL,
            sync_every_groups=SYNC_EVERY,
            verify_every_groups=VERIFY_EVERY,
            seed=SEED,
            evidence_dir=EVIDENCE_DIR,
            save_expected_file=SAVE_EXPECTED,
            keep_success_files=KEEP_SUCCESS_FILES,
            fsync_worker_count=FSYNC_WORKERS,
            fsync_batch_width=FSYNC_BATCH_WIDTH,
            verity_ratio_percent=VERITY_RATIO_PERCENT,
            verity_min_fsync_passes=VERITY_MIN_FSYNC_PASSES,
        ),
    )
    print(
        f"[summary] groups={summary.groups_done} written={summary.files_written} "
        f"verified={summary.files_verified} fsync_files={summary.fsync_files} "
        f"verity={summary.verity_enabled} evidence={summary.evidence_dir}",
        flush=True,
    )
    if summary.corrupt_label:
        raise RuntimeError(
            f"corrupt sample captured label={summary.corrupt_label} "
            f"path={summary.corrupt_path} evidence={summary.evidence_dir}"
        )


def cleanup(success: bool) -> None:
    print("[cleanup]", flush=True)
    preserve_failure = (not success) and KEEP_WORKDIR_ON_FAILURE
    lm.cleanup(verbose=True, remove_image=not preserve_failure)
    if success or not KEEP_WORKDIR_ON_FAILURE:
        shutil.rmtree(WORKDIR, ignore_errors=True)
    else:
        print(f"[cleanup] preserved workdir={WORKDIR}", flush=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")
    success = False
    try:
        enc_root = prepare()
        run(enc_root)
        success = True
    finally:
        cleanup(success)
    print("[OK] test_inlinecrypt_artifact_pressure passed", flush=True)


if __name__ == "__main__":
    main()
