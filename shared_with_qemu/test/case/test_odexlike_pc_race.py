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
from utils.odexlike_pc import VariantConfig, run_variant


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_odexlike_pc_race.")
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT = WORKDIR + "/mnt"
IMAGE_SIZE = _env_int("ODEXLIKE_IMAGE_MB", 512) * 1024 * 1024
SEED = _env_int("ODEXLIKE_SEED", 20260421)
GENERATIONS = _env_int("ODEXLIKE_GENERATIONS", 80)
PAGE_COUNT = _env_int("ODEXLIKE_PAGE_COUNT", 256)
PAGE_SLEEP_MAX_MS = _env_int("ODEXLIKE_PAGE_SLEEP_MAX_MS", 3)
PHASE_SLEEP_MAX_MS = _env_int("ODEXLIKE_PHASE_SLEEP_MAX_MS", 8)
CONSUMER_PHASE_MODE = os.getenv("ODEXLIKE_CONSUMER_PHASE_MODE", "sweep")
CONSUMER_LAUNCH_SLOT = _env_int("ODEXLIKE_CONSUMER_LAUNCH_SLOT", 0)
PRESSURE_MB = _env_int("ODEXLIKE_PRESSURE_MB", 512)


lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)
    print(f"[prepare] mounted at {MNT}", flush=True)


def run_one(cfg: VariantConfig) -> None:
    print(f"[run] variant={cfg.name} writer={cfg.writer} seed={cfg.seed}", flush=True)
    result = run_variant(MNT, cfg)
    print(
        f"[run] variant={result.name} generations={result.producer_generations} "
        f"passes={result.consumer_passes} enoent={result.enoent_retries} "
        f"mismatches={result.mismatches}",
        flush=True,
    )
    print(f"[run] logs={result.log_path}", flush=True)
    if result.producer_error:
        raise RuntimeError(f"{cfg.name}: producer error: {result.producer_error}")
    if result.consumer_error:
        raise RuntimeError(f"{cfg.name}: consumer error: {result.consumer_error}")
    if result.mismatches:
        raise RuntimeError(f"{cfg.name}: mismatches={result.mismatches}")


def run() -> None:
    variants = [
        VariantConfig(
            name="odex_like_buffered",
            writer="buffered",
            generations=GENERATIONS,
            page_count=PAGE_COUNT,
            seed=SEED,
            page_sleep_max_ms=PAGE_SLEEP_MAX_MS,
            phase_sleep_max_ms=PHASE_SLEEP_MAX_MS,
            consumer_phase_mode=CONSUMER_PHASE_MODE,
            consumer_launch_slot=CONSUMER_LAUNCH_SLOT,
            pressure_mb=PRESSURE_MB,
            flush_each_page=False,
        ),
        VariantConfig(
            name="vdex_like_mmap",
            writer="mmap",
            generations=GENERATIONS,
            page_count=PAGE_COUNT,
            seed=SEED ^ 0x55AA10,
            page_sleep_max_ms=PAGE_SLEEP_MAX_MS,
            phase_sleep_max_ms=PHASE_SLEEP_MAX_MS,
            consumer_phase_mode=CONSUMER_PHASE_MODE,
            consumer_launch_slot=CONSUMER_LAUNCH_SLOT,
            pressure_mb=PRESSURE_MB,
            flush_each_page=True,
        ),
    ]
    for cfg in variants:
        run_one(cfg)


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
    print("[OK] test_odexlike_pc_race passed", flush=True)


if __name__ == "__main__":
    main()
