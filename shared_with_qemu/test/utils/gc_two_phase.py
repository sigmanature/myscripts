import os
from dataclasses import dataclass

from .io import ensure_file_size, fsync, open_rw, pread_scan, pwrite_pattern_config, sleep_s
from .patterns import PatternConfig
from .verify import OverlaySpec, verify_file_overlays


@dataclass(frozen=True)
class TwoPhaseSpec:
    file_size: int
    read_first: int
    front_write: int
    tail_write: int
    sleep_after_front: float


def make_baseline_config(*, chunk_size: int = 256 * 1024) -> PatternConfig:
    return PatternConfig(
        mode="repeat",
        token=b"A",
        seed=0,
        chunk_size=chunk_size,
        pattern_gen="stream",
        readback="stream",
    )


def make_mod251_config(*, seed: int, chunk_size: int = 256 * 1024) -> PatternConfig:
    return PatternConfig(
        mode="mod251",
        token=b"",
        seed=int(seed),
        chunk_size=chunk_size,
        pattern_gen="stream",
        readback="stream",
    )


def init_file_with_baseline(path: str, size: int, *, baseline: PatternConfig) -> None:
    fd = open_rw(path, create=True)
    try:
        ensure_file_size(fd, size)
        pwrite_pattern_config(fd, 0, size, baseline)
        fsync(fd)
    finally:
        os.close(fd)


def run_two_phase_group_and_verify(
    path: str,
    *,
    spec: TwoPhaseSpec,
    seed_base: int,
    baseline: PatternConfig,
    chunk_size: int = 256 * 1024,
    verify_disk: bool = True,
) -> bool:
    """Run one group on the target file and verify on-disk content."""
    # 1) read first span (no data change)
    fd = open_rw(path, create=False)
    try:
        pread_scan(fd, 0, spec.read_first, chunk=chunk_size, passes=1)

        # 2) write front region
        front_cfg = make_mod251_config(seed=seed_base + 1, chunk_size=chunk_size)
        pwrite_pattern_config(fd, 0, spec.front_write, front_cfg)
        fsync(fd)

        # 3) wait
        sleep_s(spec.sleep_after_front)

        # 4) write tail region
        tail_off = spec.file_size - spec.tail_write
        tail_cfg = make_mod251_config(seed=seed_base + 2, chunk_size=chunk_size)
        pwrite_pattern_config(fd, tail_off, spec.tail_write, tail_cfg)
        fsync(fd)
    finally:
        os.close(fd)

    if not verify_disk:
        return True

    overlays = [
        OverlaySpec(offset=0, length=spec.front_write, config=front_cfg),
        OverlaySpec(offset=spec.file_size - spec.tail_write, length=spec.tail_write, config=tail_cfg),
    ]
    return verify_file_overlays(
        path,
        expected_size=spec.file_size,
        baseline=baseline,
        overlays=overlays,
        chunk_size=chunk_size,
        cold_read=True,
    )

