#!/usr/bin/env python3
import os
import random
import shutil
import sys
import tempfile
import threading
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.io import create_and_fill_file, ensure_dir
from utils.loop_mount import LoopMount
from utils.patterns import PatternConfig
from utils.short_write import ShortWriteRequest, issue_faulting_pwritev
from utils.sysutil import drop_caches
from utils.verify import OverlaySpec, verify_file_overlays


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


WORKDIR = tempfile.mkdtemp(prefix="test_short_write_buffered.")
IMAGE_PATH = WORKDIR + "/f2fs.img"
MNT = WORKDIR + "/mnt"
IMAGE_SIZE = _env_int("SHORTWRITE_IMAGE_MB", 256) * 1024 * 1024
ITERATIONS = _env_int("SHORTWRITE_ITERS", 32)
SEED = _env_int("SHORTWRITE_SEED", 20260422)
PRESSURE_MB = _env_int("SHORTWRITE_PRESSURE_MB", 0)
VERIFY_COLD = _env_int("SHORTWRITE_VERIFY_COLD", 1) != 0
BASELINE_PAGES = 8
CHUNK_SIZE = 256 * 1024

lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MNT)


class PressureWorker:
    def __init__(self, total_mb: int) -> None:
        self.total_mb = max(0, total_mb)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self.total_mb <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="shortwrite-pressure", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        chunks = []
        target = self.total_mb
        idx = 0
        while not self._stop.is_set():
            while len(chunks) < target and not self._stop.is_set():
                chunk = bytearray(1024 * 1024)
                for off in range(0, len(chunk), 4096):
                    chunk[off] = (idx + off) & 0xFF
                chunks.append(chunk)
                idx += 1
            if chunks:
                victim = idx % len(chunks)
                chunk = chunks[victim]
                for off in range(0, len(chunk), 4096):
                    chunk[off] ^= 0x5A
            time.sleep(0.005)


def _cfg(seed: int) -> PatternConfig:
    return PatternConfig(
        mode="mod251",
        token=b"",
        seed=seed,
        chunk_size=CHUNK_SIZE,
        pattern_gen="mod251",
        readback="pread",
    )


def _rand_user_shift(rng: random.Random, page_size: int) -> int:
    # Keep the valid prefix reasonably sized while still randomizing the fault boundary.
    return rng.randint(128, page_size - 256)


def _rand_invalid_tail(rng: random.Random, page_size: int) -> int:
    return rng.randint(64, min(2048, page_size))


def _run_new_file_case(rng: random.Random, idx: int, page_size: int) -> bool:
    path = os.path.join(MNT, f"new_fault_{idx:03d}.bin")
    cfg = _cfg(SEED ^ 0x110000 ^ idx)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        req = ShortWriteRequest(
            file_offset=0,
            user_shift=_rand_user_shift(rng, page_size),
            invalid_tail=_rand_invalid_tail(rng, page_size),
            config=cfg,
        )
        result = issue_faulting_pwritev(fd, req)
        os.fsync(fd)
    finally:
        os.close(fd)

    print(
        f"[new] iter={idx} shift={result.user_shift} requested={result.requested} "
        f"expected_prefix={result.expected_prefix} written={result.written} errno={result.errno_value}",
        flush=True,
    )
    if not result.short_write:
        print("[new] FAIL: syscall did not produce a positive short write", flush=True)
        return False

    os.sync()
    return verify_file_overlays(
        path,
        expected_size=result.written,
        baseline=cfg,
        overlays=[],
        chunk_size=CHUNK_SIZE,
        cold_read=VERIFY_COLD,
    )


def _run_overwrite_case(rng: random.Random, idx: int, page_size: int) -> bool:
    path = os.path.join(MNT, f"overwrite_fault_{idx:03d}.bin")
    baseline_cfg = _cfg(SEED ^ 0x220000 ^ idx)
    write_cfg = _cfg(SEED ^ 0x330000 ^ idx)
    baseline_size = BASELINE_PAGES * page_size
    create_and_fill_file(path, baseline_size, baseline_cfg)

    user_shift = _rand_user_shift(rng, page_size)
    invalid_tail = _rand_invalid_tail(rng, page_size)
    valid_prefix = page_size - user_shift
    max_off = baseline_size - valid_prefix
    file_offset = rng.randint(0, max_off)

    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        req = ShortWriteRequest(
            file_offset=file_offset,
            user_shift=user_shift,
            invalid_tail=invalid_tail,
            config=write_cfg,
        )
        result = issue_faulting_pwritev(fd, req)
        os.fsync(fd)
    finally:
        os.close(fd)

    print(
        f"[overwrite] iter={idx} file_off={result.file_offset} shift={result.user_shift} "
        f"requested={result.requested} expected_prefix={result.expected_prefix} "
        f"written={result.written} errno={result.errno_value}",
        flush=True,
    )
    if not result.short_write:
        print("[overwrite] FAIL: syscall did not produce a positive short write", flush=True)
        return False

    os.sync()
    return verify_file_overlays(
        path,
        expected_size=baseline_size,
        baseline=baseline_cfg,
        overlays=[
            OverlaySpec(
                offset=result.file_offset,
                length=result.written,
                config=write_cfg,
                overlay_off=result.file_offset,
            )
        ],
        chunk_size=CHUNK_SIZE,
        cold_read=VERIFY_COLD,
    )


def prepare() -> None:
    print("[prepare] setup image + mount", flush=True)
    ensure_dir(WORKDIR)
    lm.setup(IMAGE_SIZE, verbose=True)
    print(
        f"[prepare] mounted at {MNT} image_mb={IMAGE_SIZE // (1024 * 1024)} "
        f"iters={ITERATIONS} seed={SEED} pressure_mb={PRESSURE_MB}",
        flush=True,
    )


def run() -> None:
    rng = random.Random(SEED)
    page_size = os.sysconf("SC_PAGESIZE")
    worker = PressureWorker(PRESSURE_MB)
    worker.start()
    try:
        for idx in range(ITERATIONS):
            if not _run_new_file_case(rng, idx, page_size):
                raise RuntimeError(f"new-file case failed at iter={idx}")
            if not _run_overwrite_case(rng, idx, page_size):
                raise RuntimeError(f"overwrite case failed at iter={idx}")
    finally:
        worker.stop()


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
    print("[OK] test_short_write_buffered passed", flush=True)


if __name__ == "__main__":
    main()
