#!/usr/bin/env python3
import os
import shutil
import sys
import tempfile
import threading
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import ensure_dir
from utils.loop_mount import LoopMount
from utils.f2fs_gc import GcPulseThread
from utils.memory_pressure import MemoryPressureThread
from utils.fscrypt_inline import ensure_inline_fscrypt_dir
from utils.odexlike_pc import VariantConfig, run_variant, VariantResult


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_odexlike_gc_race.")
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT = WORKDIR + "/mnt"
IMAGE_SIZE_MB = _env_int("GC_RACE_IMAGE_MB", 1024)
IMAGE_SIZE = IMAGE_SIZE_MB * 1024 * 1024

ENC_DIR_NAME = "enc_test"
FSCRYPT_KEY = "/opt/test-secrets/fscrypt-ci.key"
PROTECTOR_NAME = "gc-race-inline"

SEED = _env_int("GC_RACE_SEED", 20260701)
PAGE_COUNT = _env_int("GC_RACE_PAGE_COUNT", 256)
PAGE_SLEEP_MAX_MS = _env_int("GC_RACE_PAGE_SLEEP_MAX_MS", 2)
PHASE_SLEEP_MAX_MS = _env_int("GC_RACE_PHASE_SLEEP_MAX_MS", 6)
GENERATIONS = _env_int("GC_RACE_GENERATIONS", 200)
TAIL_BYTES = _env_int("GC_RACE_TAIL_BYTES", 2048)

GC_INTERVAL_S = 0.2
PRESSURE_MB = _env_int("GC_RACE_PRESSURE_MB", 384)
VERBOSE = True


def run_one(cfg: VariantConfig, work_dir: str) -> VariantResult:
    t0 = time.time()
    result = run_variant(work_dir, cfg)
    dt = time.time() - t0
    print(
        f"  [{cfg.name}] gen={result.producer_generations} passes={result.consumer_passes} "
        f"mismatches={result.mismatches} dt={dt:.1f}s",
        flush=True,
    )
    if result.mismatches:
        raise RuntimeError(f"{cfg.name}: mismatches={result.mismatches}")
    return result


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("need root")

    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)

    lm = LoopMount(
        image_path=IMAGE_PATH,
        mountpoint=MNT,
        mount_opts="mode=lfs,inlinecrypt",
        mkfs_features=("encrypt",),
    )
    lm.setup(IMAGE_SIZE, verbose=VERBOSE)

    enc_root = ensure_inline_fscrypt_dir(
        mount_root=MNT,
        enc_dir_name=ENC_DIR_NAME,
        key_path=FSCRYPT_KEY,
        protector_name=PROTECTOR_NAME,
    )
    print(f"[prepare] enc_root={enc_root}", flush=True)

    gc_thr = GcPulseThread(mountpoint=MNT, interval_s=GC_INTERVAL_S, verbose=VERBOSE)
    gc_thr.start()
    print(f"[prepare] gc backend: {gc_thr.backend_desc}", flush=True)

    mp_thr = MemoryPressureThread(bytes_target=PRESSURE_MB * 1024 * 1024, seed=SEED ^ 0x1)
    mp_thr.start()
    print(f"[prepare] memory pressure: {PRESSURE_MB}MB", flush=True)

    odex_cfg = VariantConfig(
        name="odex_like_buffered",
        writer="buffered",
        generations=GENERATIONS,
        page_count=PAGE_COUNT,
        seed=SEED,
        page_sleep_max_ms=PAGE_SLEEP_MAX_MS,
        phase_sleep_max_ms=PHASE_SLEEP_MAX_MS,
        consumer_phase_mode="sweep",
        consumer_launch_slot=0,
        pressure_mb=0,
        flush_each_page=False,
        tail_bytes=TAIL_BYTES,
    )
    vdex_cfg = VariantConfig(
        name="vdex_like_mmap",
        writer="mmap",
        generations=GENERATIONS,
        page_count=PAGE_COUNT,
        seed=SEED ^ 0x55AA10,
        page_sleep_max_ms=PAGE_SLEEP_MAX_MS,
        phase_sleep_max_ms=PHASE_SLEEP_MAX_MS,
        consumer_phase_mode="sweep",
        consumer_launch_slot=0,
        pressure_mb=0,
        flush_each_page=True,
        tail_bytes=TAIL_BYTES,
    )

    print(f"\n[run] concurrent odex+vdex in {enc_root}", flush=True)
    t0 = time.time()

    threads = []
    results: dict[str, VariantResult] = {}
    errors: list[str] = []

    def _run_one(cfg: VariantConfig) -> None:
        try:
            results[cfg.name] = run_one(cfg, enc_root)
        except Exception as e:
            errors.append(f"{cfg.name}: {e}")

    for cfg in (odex_cfg, vdex_cfg):
        th = threading.Thread(target=_run_one, args=(cfg,), daemon=True)
        threads.append(th)
        th.start()

    for th in threads:
        th.join()

    dt = time.time() - t0
    gc_thr.stop()
    mp_thr.stop()

    print(f"\n[*] total dt={dt:.1f}s gc_pulses={gc_thr.pulses}/{gc_thr.success}", flush=True)

    if errors:
        raise SystemExit(f"errors: {errors}")

    print(f"[cleanup]", flush=True)
    lm.cleanup(verbose=VERBOSE)
    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("[OK] test_odexlike_gc_race passed", flush=True)


if __name__ == "__main__":
    main()
