import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable

from .churn import ChurnThread
from .io import create_and_fill_file, ensure_dir, fsync, open_rw, pwrite_pattern_config
from .memory_pressure import MemoryPressureThread
from .patterns import PatternConfig
from .verify import OverlaySpec, VerifyEvidenceConfig, verify_file_overlays


@dataclass(frozen=True)
class RetryRaceConfig:
    target_file_count: int
    file_size_bytes: int
    fsync_chunk_bytes: int
    background_chunk_bytes: int
    runtime_sec: int
    fsync_workers: int
    background_writers: int
    fsync_pause_ms: int
    background_pause_ms: int
    dirty_background_bytes: int
    dirty_bytes: int
    dirty_expire_centisecs: int
    dirty_writeback_centisecs: int
    retry_force_loops: int
    retry_force_mode: int
    filter_target_inos: bool
    sysrq_interval_s: float
    stall_timeout_s: float
    sysrq_sequence: tuple[str, ...]
    memory_pressure_bytes: int
    churn_files_per_round: int
    churn_file_bytes: int
    churn_keep_fraction: float
    churn_interval_s: float
    seed: int


@dataclass
class RetryRaceSummary:
    target_inos: tuple[int, ...]
    target_paths: tuple[str, ...]
    validation_plans: tuple["RetryRaceValidationPlan", ...]
    fsync_ops: int = 0
    background_ops: int = 0
    retry_enter_count: int = 0
    retry_clean_count: int = 0
    retry_noclean_count: int = 0
    sync_all_events: int = 0
    sync_none_events: int = 0
    sysrq_dumps: int = 0
    stall_detected: bool = False
    log_path: str = ""
    dmesg_path: str = ""


@dataclass(frozen=True)
class RetryRaceValidationPlan:
    path: str
    expected_size: int
    baseline: PatternConfig
    overlays: tuple[OverlaySpec, ...]


def _mod_cfg(seed: int, chunk_size: int = 256 * 1024) -> PatternConfig:
    return PatternConfig(
        mode="mod251",
        token=b"",
        seed=seed,
        chunk_size=chunk_size,
        pattern_gen="stream",
        readback="pread",
    )


def assigned_worker_slots(total_slots: int, worker_id: int, worker_count: int) -> tuple[int, ...]:
    if total_slots <= 0:
        raise ValueError(f"total_slots must be > 0, got {total_slots}")
    if worker_count <= 0:
        raise ValueError(f"worker_count must be > 0, got {worker_count}")
    if worker_id < 0 or worker_id >= worker_count:
        raise ValueError(f"invalid worker_id={worker_id} for worker_count={worker_count}")
    if worker_count > total_slots:
        raise ValueError(
            f"worker_count={worker_count} exceeds total_slots={total_slots}; "
            "would make content verification ambiguous"
        )
    return tuple(range(worker_id, total_slots, worker_count))


def _mount_source(mountpoint: str) -> str:
    mp = os.path.realpath(mountpoint)
    with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fp:
        for line in fp:
            parts = line.split()
            if len(parts) >= 3 and os.path.realpath(parts[1]) == mp and parts[2] == "f2fs":
                return parts[0]
    raise RuntimeError(f"mount source not found for {mountpoint}")


def _sysfs_dir_for_mount(mountpoint: str) -> str:
    source = _mount_source(mountpoint)
    sysfs_dir = os.path.join("/sys/fs/f2fs", os.path.basename(os.path.realpath(source)))
    if not os.path.isdir(sysfs_dir):
        raise RuntimeError(f"f2fs sysfs dir missing: {sysfs_dir}")
    return sysfs_dir


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fp:
        return fp.read().strip()


def _write_text(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as fp:
        fp.write(str(value))


def _append_log(log_path: str, line: str, lock: threading.Lock) -> None:
    stamp = time.strftime("%H:%M:%S")
    with lock:
        with open(log_path, "a", encoding="utf-8") as fp:
            fp.write(f"[{stamp}] {line}\n")


def _write_kmsg_marker(tag: str) -> None:
    with open("/dev/kmsg", "w", encoding="ascii") as fp:
        fp.write(f"retry_race_marker {tag}\n")


def _dmesg_since_marker(tag: str) -> str:
    cp = subprocess.run(["dmesg"], check=True, capture_output=True, text=True)
    marker = f"retry_race_marker {tag}"
    lines = cp.stdout.splitlines()
    start = 0
    for idx, line in enumerate(lines):
        if marker in line:
            start = idx + 1
    return "\n".join(lines[start:]) + "\n"


class KernelLogCapture:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fp = None
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._fp = open(self.path, "w", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                ["dmesg", "--follow-new"],
                stdout=self._fp,
                stderr=subprocess.PIPE,
                text=True,
            )
        except BaseException:
            self._fp.close()
            self._fp = None
            raise

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5.0)
        finally:
            if self._fp is not None:
                self._fp.flush()
                self._fp.close()
                self._fp = None
            self._proc = None


class ProgressTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_progress = time.monotonic()
        self.fsync_ops = 0
        self.background_ops = 0

    def note_fsync(self) -> None:
        with self._lock:
            self.fsync_ops += 1
            self._last_progress = time.monotonic()

    def note_background(self) -> None:
        with self._lock:
            self.background_ops += 1
            self._last_progress = time.monotonic()

    def snapshot(self) -> tuple[int, int, float]:
        with self._lock:
            return self.fsync_ops, self.background_ops, self._last_progress


class LastWriteLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._entries: dict[tuple[int, int, int], tuple[int, OverlaySpec]] = {}

    def note_write(self, file_idx: int, offset: int, length: int, config: PatternConfig) -> None:
        with self._lock:
            self._seq += 1
            self._entries[(file_idx, offset, length)] = (
                self._seq,
                OverlaySpec(offset=offset, length=length, config=config),
            )

    def overlays_for(self, file_idx: int) -> tuple[OverlaySpec, ...]:
        with self._lock:
            items = [
                item
                for (idx, _, _), item in self._entries.items()
                if idx == file_idx
            ]
        items.sort(key=lambda item: item[0])
        return tuple(spec for _, spec in items)


class VmDirtyTuning:
    _FILES = (
        "dirty_bytes",
        "dirty_background_bytes",
        "dirty_ratio",
        "dirty_background_ratio",
        "dirty_expire_centisecs",
        "dirty_writeback_centisecs",
    )

    def __init__(self, cfg: RetryRaceConfig) -> None:
        self.cfg = cfg
        self._saved: dict[str, str] = {}

    def apply(self) -> None:
        values = {
            "dirty_bytes": str(self.cfg.dirty_bytes),
            "dirty_background_bytes": str(self.cfg.dirty_background_bytes),
            "dirty_expire_centisecs": str(self.cfg.dirty_expire_centisecs),
            "dirty_writeback_centisecs": str(self.cfg.dirty_writeback_centisecs),
        }
        for name in self._FILES:
            path = os.path.join("/proc/sys/vm", name)
            self._saved[name] = _read_text(path)
            if name in values:
                _write_text(path, values[name])

    def restore(self) -> None:
        dirty_path = os.path.join("/proc/sys/vm", "dirty_bytes")
        bg_path = os.path.join("/proc/sys/vm", "dirty_background_bytes")
        target_dirty = int(self._saved["dirty_bytes"])
        target_bg = int(self._saved["dirty_background_bytes"])
        target_ratio = int(self._saved["dirty_ratio"])
        target_bg_ratio = int(self._saved["dirty_background_ratio"])
        current_dirty = int(_read_text(dirty_path))

        if target_ratio and target_bg_ratio > target_ratio:
            raise RuntimeError(
                "invalid saved dirty vm ratio state: "
                f"dirty_background_ratio={target_bg_ratio} > dirty_ratio={target_ratio}"
            )

        if target_dirty and target_bg > target_dirty:
            raise RuntimeError(
                "invalid saved dirty vm state: "
                f"dirty_background_bytes={target_bg} > dirty_bytes={target_dirty}"
            )

        if target_dirty == 0 and target_bg == 0:
            dirty_ratio_path = os.path.join("/proc/sys/vm", "dirty_ratio")
            bg_ratio_path = os.path.join("/proc/sys/vm", "dirty_background_ratio")

            # Switch back to ratio mode instead of trying to write 0 to *_bytes.
            if target_ratio and target_bg_ratio <= target_ratio:
                _write_text(dirty_ratio_path, str(target_ratio))
                _write_text(bg_ratio_path, str(target_bg_ratio))
            else:
                _write_text(bg_ratio_path, str(target_bg_ratio))
                _write_text(dirty_ratio_path, str(target_ratio))
        else:
            # Keep dirty_background_bytes <= dirty_bytes across the whole restore sequence.
            if target_bg <= current_dirty:
                _write_text(bg_path, str(target_bg))
                _write_text(dirty_path, str(target_dirty))
            else:
                _write_text(dirty_path, str(target_dirty))
                _write_text(bg_path, str(target_bg))

        for name in ("dirty_expire_centisecs", "dirty_writeback_centisecs"):
            path = os.path.join("/proc/sys/vm", name)
            _write_text(path, self._saved[name])


class RetryDebugKnobs:
    _FILES = (
        "dbg_wcf_ino1",
        "dbg_wcf_ino2",
        "dbg_wcf_verity_only",
        "dbg_wcf_retry_force_loops",
        "dbg_wcf_retry_force_mode",
    )

    def __init__(self, mountpoint: str, target_inos: Iterable[int], cfg: RetryRaceConfig) -> None:
        self.sysfs_dir = _sysfs_dir_for_mount(mountpoint)
        self.target_inos = tuple(int(x) for x in target_inos)
        self.cfg = cfg
        self._saved: dict[str, str] = {}

    def apply(self) -> None:
        values = {
            "dbg_wcf_ino1": str(
                self.target_inos[0] if self.cfg.filter_target_inos and len(self.target_inos) >= 1 else 0
            ),
            "dbg_wcf_ino2": str(
                self.target_inos[1] if self.cfg.filter_target_inos and len(self.target_inos) >= 2 else 0
            ),
            "dbg_wcf_verity_only": "0",
            "dbg_wcf_retry_force_loops": str(self.cfg.retry_force_loops),
            "dbg_wcf_retry_force_mode": str(self.cfg.retry_force_mode),
        }
        for name in self._FILES:
            path = os.path.join(self.sysfs_dir, name)
            if not os.path.exists(path):
                raise RuntimeError(f"required debug sysfs knob missing: {path}")
            self._saved[name] = _read_text(path)
            _write_text(path, values[name])

    def restore(self) -> None:
        for name, value in self._saved.items():
            path = os.path.join(self.sysfs_dir, name)
            _write_text(path, value)


class FsyncWorkerThread(threading.Thread):
    def __init__(
        self,
        worker_id: int,
        paths: tuple[str, ...],
        cfg: RetryRaceConfig,
        tracker: ProgressTracker,
        ledger: LastWriteLedger,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.paths = paths
        self.cfg = cfg
        self.tracker = tracker
        self.ledger = ledger
        self.stop_event = stop_event
        self.seed = cfg.seed + 0x1000 + worker_id * 97
        slots = max(1, (self.cfg.file_size_bytes // 2) // self.cfg.fsync_chunk_bytes)
        self.assigned_slots = assigned_worker_slots(slots, worker_id, cfg.fsync_workers)
        self.write_seq = 0

    def run(self) -> None:
        rng = random.Random(self.seed)
        fds = [open_rw(path, create=False) for path in self.paths]
        try:
            while not self.stop_event.is_set():
                idx = rng.randrange(len(fds))
                slot = self.assigned_slots[rng.randrange(len(self.assigned_slots))]
                offset = slot * self.cfg.fsync_chunk_bytes
                self.write_seq += 1
                write_cfg = _mod_cfg(self.seed + self.write_seq * 257 + idx)
                pwrite_pattern_config(
                    fds[idx],
                    offset,
                    self.cfg.fsync_chunk_bytes,
                    write_cfg,
                )
                fsync(fds[idx])
                self.ledger.note_write(idx, offset, self.cfg.fsync_chunk_bytes, write_cfg)
                self.tracker.note_fsync()
                if self.cfg.fsync_pause_ms > 0:
                    time.sleep(self.cfg.fsync_pause_ms / 1000.0)
        finally:
            for fd in fds:
                os.close(fd)


class BackgroundDirtyThread(threading.Thread):
    def __init__(
        self,
        worker_id: int,
        paths: tuple[str, ...],
        cfg: RetryRaceConfig,
        tracker: ProgressTracker,
        ledger: LastWriteLedger,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.paths = paths
        self.cfg = cfg
        self.tracker = tracker
        self.ledger = ledger
        self.stop_event = stop_event
        self.seed = cfg.seed + 0x4000 + worker_id * 131
        half = self.cfg.file_size_bytes // 2
        span = max(self.cfg.background_chunk_bytes, self.cfg.file_size_bytes - half)
        slots = max(1, span // self.cfg.background_chunk_bytes)
        self.assigned_slots = assigned_worker_slots(slots, worker_id, cfg.background_writers)
        self.write_seq = 0

    def run(self) -> None:
        rng = random.Random(self.seed)
        fds = [open_rw(path, create=False) for path in self.paths]
        try:
            half = self.cfg.file_size_bytes // 2
            while not self.stop_event.is_set():
                idx = rng.randrange(len(fds))
                slot = self.assigned_slots[rng.randrange(len(self.assigned_slots))]
                offset = half + slot * self.cfg.background_chunk_bytes
                if offset + self.cfg.background_chunk_bytes > self.cfg.file_size_bytes:
                    offset = self.cfg.file_size_bytes - self.cfg.background_chunk_bytes
                self.write_seq += 1
                write_cfg = _mod_cfg(self.seed + self.write_seq * 257 + idx)
                pwrite_pattern_config(
                    fds[idx],
                    offset,
                    self.cfg.background_chunk_bytes,
                    write_cfg,
                )
                self.ledger.note_write(idx, offset, self.cfg.background_chunk_bytes, write_cfg)
                self.tracker.note_background()
                if self.cfg.background_pause_ms > 0:
                    time.sleep(self.cfg.background_pause_ms / 1000.0)
        finally:
            for fd in fds:
                os.close(fd)


class SysrqWatcherThread(threading.Thread):
    def __init__(
        self,
        log_path: str,
        cfg: RetryRaceConfig,
        tracker: ProgressTracker,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True)
        self.log_path = log_path
        self.cfg = cfg
        self.tracker = tracker
        self.stop_event = stop_event
        self.lock = threading.Lock()
        self.dumps = 0
        self.stall_detected = False

    def _dump(self, reason: str) -> None:
        self.dumps += 1
        _append_log(self.log_path, f"sysrq_dump reason={reason} seq={''.join(self.cfg.sysrq_sequence)}", self.lock)
        _write_kmsg_marker(f"sysrq_begin_{self.dumps}_{reason}")
        for key in self.cfg.sysrq_sequence:
            with open("/proc/sysrq-trigger", "w", encoding="ascii") as fp:
                fp.write(key)
            time.sleep(0.2)
        _write_kmsg_marker(f"sysrq_end_{self.dumps}_{reason}")

    def run(self) -> None:
        next_periodic = time.monotonic() + self.cfg.sysrq_interval_s
        while not self.stop_event.wait(1.0):
            _, _, last_progress = self.tracker.snapshot()
            now = time.monotonic()
            if now >= next_periodic:
                self._dump("periodic")
                next_periodic = now + self.cfg.sysrq_interval_s
                continue
            if now - last_progress >= self.cfg.stall_timeout_s and not self.stall_detected:
                self.stall_detected = True
                self._dump("stall")


def _prepare_targets(root: str, cfg: RetryRaceConfig) -> tuple[str, ...]:
    ensure_dir(root)
    paths = []
    for idx in range(cfg.target_file_count):
        path = os.path.join(root, f"target_{idx:02d}.retry.bin")
        create_and_fill_file(path, cfg.file_size_bytes, _mod_cfg(cfg.seed + idx))
        paths.append(path)
    return tuple(paths)


def _count_lines(blob: str, needle: str, inos: tuple[int, ...], filter_inos: bool) -> int:
    total = 0
    for line in blob.splitlines():
        if needle not in line:
            continue
        if not filter_inos or any(f"ino={ino}" in line for ino in inos):
            total += 1
    return total


def _build_validation_plans(
    target_paths: tuple[str, ...],
    cfg: RetryRaceConfig,
    ledger: LastWriteLedger,
) -> tuple[RetryRaceValidationPlan, ...]:
    plans = []
    for idx, path in enumerate(target_paths):
        plans.append(
            RetryRaceValidationPlan(
                path=path,
                expected_size=cfg.file_size_bytes,
                baseline=_mod_cfg(cfg.seed + idx),
                overlays=ledger.overlays_for(idx),
            )
        )
    return tuple(plans)


def verify_retry_race_summary(
    summary: RetryRaceSummary,
    *,
    phase: str,
    evidence_root: str,
) -> None:
    ensure_dir(evidence_root)
    for plan in summary.validation_plans:
        label = f"{phase}_{os.path.basename(plan.path)}"
        ok = verify_file_overlays(
            plan.path,
            expected_size=plan.expected_size,
            baseline=plan.baseline,
            overlays=plan.overlays,
            chunk_size=256 * 1024,
            cold_read=True,
            evidence=VerifyEvidenceConfig(out_dir=evidence_root, label=label),
        )
        if not ok:
            raise RuntimeError(f"{phase} content verification failed: {plan.path}")


def run_retry_fsync_writeback_race(workdir: str, mountpoint: str, cfg: RetryRaceConfig) -> RetryRaceSummary:
    fsync_slots = max(1, (cfg.file_size_bytes // 2) // cfg.fsync_chunk_bytes)
    assigned_worker_slots(fsync_slots, 0, cfg.fsync_workers)
    background_span = max(cfg.background_chunk_bytes, cfg.file_size_bytes - (cfg.file_size_bytes // 2))
    background_slots = max(1, background_span // cfg.background_chunk_bytes)
    assigned_worker_slots(background_slots, 0, cfg.background_writers)

    targets_dir = os.path.join(mountpoint, "targets")
    churn_dir = os.path.join(mountpoint, "churn")
    logs_dir = os.path.join(workdir, "logs")
    ensure_dir(logs_dir)

    target_paths = _prepare_targets(targets_dir, cfg)
    target_inos = tuple(os.stat(path).st_ino for path in target_paths)
    marker = f"retry_race_begin_{int(time.time())}_{os.getpid()}"
    _write_kmsg_marker(marker)

    tracker = ProgressTracker()
    ledger = LastWriteLedger()
    stop_event = threading.Event()
    log_path = os.path.join(logs_dir, "retry_race.log")
    dmesg_path = os.path.join(logs_dir, "retry_race.dmesg")
    dirty_tuning = VmDirtyTuning(cfg)
    debug_knobs = RetryDebugKnobs(mountpoint, target_inos, cfg)
    dmesg_blob = ""
    klog = KernelLogCapture(dmesg_path)

    mem_thread = MemoryPressureThread(
        cfg.memory_pressure_bytes,
        seed=cfg.seed ^ 0x55AA,
    )
    churn_thread = ChurnThread(
        churn_dir=churn_dir,
        inline_mode=False,
        file_size=cfg.churn_file_bytes,
        files_per_round=cfg.churn_files_per_round,
        keep_fraction=cfg.churn_keep_fraction,
        interval_s=cfg.churn_interval_s,
        seed=cfg.seed ^ 0xA55A,
        verbose=False,
    )
    sysrq_thread = SysrqWatcherThread(log_path, cfg, tracker, stop_event)
    workers = [
        FsyncWorkerThread(i, target_paths, cfg, tracker, ledger, stop_event)
        for i in range(cfg.fsync_workers)
    ] + [
        BackgroundDirtyThread(i, target_paths, cfg, tracker, ledger, stop_event)
        for i in range(cfg.background_writers)
    ]

    body_exc: BaseException | None = None
    dirty_tuning.apply()
    debug_knobs.apply()
    klog.start()
    try:
        if cfg.memory_pressure_bytes > 0:
            mem_thread.start()
        if cfg.churn_files_per_round > 0:
            churn_thread.start()
        sysrq_thread.start()
        for worker in workers:
            worker.start()
        time.sleep(cfg.runtime_sec)
    except BaseException as exc:
        body_exc = exc
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        stop_event.set()
        for worker in workers:
            worker.join(timeout=5.0)
        sysrq_thread.join(timeout=5.0)
        if cfg.churn_files_per_round > 0:
            churn_thread.stop()
            churn_thread.join(timeout=5.0)
        if cfg.memory_pressure_bytes > 0:
            mem_thread.stop()
            mem_thread.join(timeout=5.0)
        try:
            klog.stop()
            with open(dmesg_path, "r", encoding="utf-8", errors="replace") as fp:
                dmesg_blob = fp.read()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            debug_knobs.restore()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            dirty_tuning.restore()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            for exc in cleanup_errors:
                _append_log(log_path, f"cleanup_error type={type(exc).__name__} err={exc}", sysrq_thread.lock)

    fsync_ops, background_ops, _ = tracker.snapshot()
    validation_plans = _build_validation_plans(target_paths, cfg, ledger)

    return RetryRaceSummary(
        target_inos=target_inos,
        target_paths=target_paths,
        validation_plans=validation_plans,
        fsync_ops=fsync_ops,
        background_ops=background_ops,
        retry_enter_count=_count_lines(
            dmesg_blob, "write_cache_folios_retry_enter", target_inos, cfg.filter_target_inos
        ),
        retry_clean_count=_count_lines(
            dmesg_blob, "write_cache_folios_retry_clean", target_inos, cfg.filter_target_inos
        ),
        retry_noclean_count=_count_lines(
            dmesg_blob, "write_cache_folios_retry_noclean", target_inos, cfg.filter_target_inos
        ),
        sync_all_events=_count_lines(
            dmesg_blob, "wbc_sync=1", target_inos, cfg.filter_target_inos
        ),
        sync_none_events=_count_lines(
            dmesg_blob, "wbc_sync=0", target_inos, cfg.filter_target_inos
        ),
        sysrq_dumps=sysrq_thread.dumps,
        stall_detected=sysrq_thread.stall_detected,
        log_path=log_path,
        dmesg_path=dmesg_path,
    )
