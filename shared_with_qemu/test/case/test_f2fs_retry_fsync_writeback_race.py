#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.f2fs_retry_race import (
    RetryRaceConfig,
    RetryRaceSummary,
    run_retry_fsync_writeback_race,
    verify_retry_race_summary,
)
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
WORKDIR = tempfile.mkdtemp(prefix="test_f2fs_retry_race.")
IMAGE_PATH = os.path.join(WORKDIR, "f2fs.img")
MNT = os.path.join(WORKDIR, "mnt")
IMAGE_SIZE = _env_int("RETRY_RACE_IMAGE_MB", 1024) * 1024 * 1024

TARGET_FILE_COUNT = _env_int("RETRY_RACE_TARGET_FILES", 2)
FILE_SIZE_MB = _env_int("RETRY_RACE_FILE_MB", 64)
FSYNC_CHUNK_MB = _env_int("RETRY_RACE_FSYNC_CHUNK_MB", 2)
BACKGROUND_CHUNK_MB = _env_int("RETRY_RACE_BACKGROUND_CHUNK_MB", 2)
RUNTIME_SEC = _env_int("RETRY_RACE_RUNTIME_SEC", 45)
FSYNC_WORKERS = _env_int("RETRY_RACE_FSYNC_WORKERS", 4)
BACKGROUND_WRITERS = _env_int("RETRY_RACE_BACKGROUND_WRITERS", 2)
FSYNC_PAUSE_MS = _env_int("RETRY_RACE_FSYNC_PAUSE_MS", 2)
BACKGROUND_PAUSE_MS = _env_int("RETRY_RACE_BACKGROUND_PAUSE_MS", 1)
DIRTY_BACKGROUND_MB = _env_int("RETRY_RACE_DIRTY_BACKGROUND_MB", 8)
DIRTY_MB = _env_int("RETRY_RACE_DIRTY_MB", 24)
DIRTY_EXPIRE_CSEC = _env_int("RETRY_RACE_DIRTY_EXPIRE_CSEC", 100)
DIRTY_WRITEBACK_CSEC = _env_int("RETRY_RACE_DIRTY_WRITEBACK_CSEC", 50)
MEMORY_PRESSURE_MB = _env_int("RETRY_RACE_MEMORY_MB", 512)
CHURN_FILES_PER_ROUND = _env_int("RETRY_RACE_CHURN_FILES", 8)
CHURN_FILE_KB = _env_int("RETRY_RACE_CHURN_FILE_KB", 512)
CHURN_KEEP = _env_float("RETRY_RACE_CHURN_KEEP", 0.25)
CHURN_INTERVAL = _env_float("RETRY_RACE_CHURN_INTERVAL", 0.05)
SYSRQ_INTERVAL_S = _env_float("RETRY_RACE_SYSRQ_INTERVAL", 15.0)
STALL_TIMEOUT_S = _env_float("RETRY_RACE_STALL_TIMEOUT", 12.0)
SEED = _env_int("RETRY_RACE_SEED", 20260504)

RETRY_FORCE_LOOPS = _env_int("RETRY_RACE_FORCE_LOOPS", 2)
RETRY_FORCE_MODE = _env_int("RETRY_RACE_FORCE_MODE", 3)
FILTER_TARGET_INOS = _env_int("RETRY_RACE_FILTER_TARGET_INOS", 0) != 0
MIN_RETRY_EVENTS = _env_int("RETRY_RACE_MIN_RETRY_EVENTS", 4)
SYSRQ_SEQUENCE = ("w", "t", "l")
KEEP_WORKDIR_ON_FAILURE = _env_int("RETRY_RACE_KEEP_WORKDIR_ON_FAILURE", 1) != 0

lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT, mount_opts="mode=lfs")


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)


def run() -> None:
    summary = run_retry_fsync_writeback_race(
        WORKDIR,
        MNT,
        RetryRaceConfig(
            target_file_count=TARGET_FILE_COUNT,
            file_size_bytes=FILE_SIZE_MB * 1024 * 1024,
            fsync_chunk_bytes=FSYNC_CHUNK_MB * 1024 * 1024,
            background_chunk_bytes=BACKGROUND_CHUNK_MB * 1024 * 1024,
            runtime_sec=RUNTIME_SEC,
            fsync_workers=FSYNC_WORKERS,
            background_writers=BACKGROUND_WRITERS,
            fsync_pause_ms=FSYNC_PAUSE_MS,
            background_pause_ms=BACKGROUND_PAUSE_MS,
            dirty_background_bytes=DIRTY_BACKGROUND_MB * 1024 * 1024,
            dirty_bytes=DIRTY_MB * 1024 * 1024,
            dirty_expire_centisecs=DIRTY_EXPIRE_CSEC,
            dirty_writeback_centisecs=DIRTY_WRITEBACK_CSEC,
            retry_force_loops=RETRY_FORCE_LOOPS,
            retry_force_mode=RETRY_FORCE_MODE,
            filter_target_inos=FILTER_TARGET_INOS,
            sysrq_interval_s=SYSRQ_INTERVAL_S,
            stall_timeout_s=STALL_TIMEOUT_S,
            sysrq_sequence=SYSRQ_SEQUENCE,
            memory_pressure_bytes=MEMORY_PRESSURE_MB * 1024 * 1024,
            churn_files_per_round=CHURN_FILES_PER_ROUND,
            churn_file_bytes=CHURN_FILE_KB * 1024,
            churn_keep_fraction=CHURN_KEEP,
            churn_interval_s=CHURN_INTERVAL,
            seed=SEED,
        ),
    )
    print(
        f"[summary] inos={summary.target_inos} fsync_ops={summary.fsync_ops} "
        f"background_ops={summary.background_ops} retry_enter={summary.retry_enter_count} "
        f"retry_clean={summary.retry_clean_count} retry_noclean={summary.retry_noclean_count} "
        f"sync_all={summary.sync_all_events} sync_none={summary.sync_none_events} "
        f"sysrq={summary.sysrq_dumps} stall={int(summary.stall_detected)}",
        flush=True,
    )
    print(f"[summary] log={summary.log_path}", flush=True)
    print(f"[summary] dmesg={summary.dmesg_path}", flush=True)
    verify_content(summary)
    if summary.retry_enter_count < MIN_RETRY_EVENTS:
        raise RuntimeError(
            f"retry events too low: got={summary.retry_enter_count} need>={MIN_RETRY_EVENTS}"
        )
    if summary.sync_all_events == 0 or summary.sync_none_events == 0:
        raise RuntimeError(
            f"missing concurrent sync/background writeback evidence: "
            f"sync_all={summary.sync_all_events} sync_none={summary.sync_none_events}"
        )
    if summary.retry_clean_count + summary.retry_noclean_count == 0:
        raise RuntimeError("missing retry clean/noclean evidence")
    if summary.stall_detected:
        raise RuntimeError("possible deadlock or hang detected; inspect sysrq output")


def verify_content(summary: RetryRaceSummary) -> None:
    verify_dir = os.path.join(WORKDIR, "verify")
    print("[verify] mounted content", flush=True)
    verify_retry_race_summary(summary, phase="mounted", evidence_root=verify_dir)

    print("[verify] remount content", flush=True)
    if not lm.unmount(verbose=True, retries=10, delay_s=0.5):
        raise RuntimeError(f"failed to unmount {MNT} for remount verification")
    lm.mount_existing(verbose=True)
    verify_retry_race_summary(summary, phase="remount", evidence_root=verify_dir)


def cleanup(success: bool) -> None:
    print("[cleanup]", flush=True)
    preserve = (not success) and KEEP_WORKDIR_ON_FAILURE
    lm.cleanup(verbose=True, remove_image=not preserve)
    if preserve:
        print(f"[cleanup] preserved workdir={WORKDIR}", flush=True)
    else:
        shutil.rmtree(WORKDIR, ignore_errors=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")
    success = False
    try:
        prepare()
        run()
        success = True
    finally:
        cleanup(success)
    print("[OK] test_f2fs_retry_fsync_writeback_race passed", flush=True)


if __name__ == "__main__":
    main()
