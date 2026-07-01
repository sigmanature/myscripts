import mmap
import json
import os
import random
import signal
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from .churn import ChurnThread
from .f2fs_gc import GcPulseThread
from .fsverity import enable_fsverity
from .io import ensure_dir, fsync
from .memory_pressure import MemoryPressureThread
from .patterns import PatternConfig, render_pattern_bytes
from .sysutil import drop_caches
from .verify import OverlaySpec, VerifyEvidenceConfig, verify_file_overlays


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass(frozen=True)
class ArtifactPressureConfig:
    groups: int
    app_count: int
    artifacts_per_app: int
    artifact_min_bytes: int
    artifact_max_bytes: int
    prefill_percent: int
    prefill_file_bytes: int
    churn_files_per_round: int
    churn_file_bytes: int
    churn_keep_fraction: float
    churn_interval_s: float
    memory_pressure_bytes: int
    gc_interval_s: float
    sync_every_groups: int
    verify_every_groups: int
    seed: int
    evidence_dir: str
    save_expected_file: bool
    keep_success_files: bool
    fsync_worker_count: int = 0
    fsync_batch_width: int = 0
    verity_ratio_percent: int = 0
    verity_min_fsync_passes: int = 1
    verity_name_tag: str = "V"


@dataclass(frozen=True)
class ArtifactRecipe:
    expected_size: int
    baseline: PatternConfig
    overlays: tuple[OverlaySpec, ...]


@dataclass
class ArtifactPressureSummary:
    groups_done: int = 0
    files_written: int = 0
    files_verified: int = 0
    fsync_batches: int = 0
    fsync_files: int = 0
    verity_enabled: int = 0
    corrupt_label: str = ""
    corrupt_path: str = ""
    evidence_dir: str = ""


@dataclass
class ArtifactState:
    recipe: ArtifactRecipe
    sync_passes: int = 0
    verity_enabled: bool = False
    verity_candidate: bool = False


@dataclass(frozen=True)
class ArtifactPlan:
    recipe: ArtifactRecipe
    body_cfg: PatternConfig
    header_cfg: PatternConfig
    extra_cfg: PatternConfig
    aligned_size: int
    final_size: int


class ArtifactWriteProcessError(RuntimeError):
    def __init__(self, path: str, trace_path: str, status: int):
        self.path = path
        self.trace_path = trace_path
        self.status = status
        self.exit_code = os.waitstatus_to_exitcode(status)
        self.signal_num = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        if self.signal_num is not None:
            try:
                sig_name = signal.Signals(self.signal_num).name
            except ValueError:
                sig_name = f"SIG{self.signal_num}"
            msg = f"artifact tmp writer died with signal {self.signal_num} ({sig_name}) path={path}"
        else:
            msg = f"artifact tmp writer exited rc={self.exit_code} path={path}"
        super().__init__(msg)


def _mod_cfg(seed: int, chunk_size: int = 256 * 1024) -> PatternConfig:
    return PatternConfig(
        mode="mod251",
        token=b"",
        seed=seed,
        chunk_size=chunk_size,
        pattern_gen="stream",
        readback="pread",
    )


def _fsync_dir(path: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        fsync(fd)
    finally:
        os.close(fd)


def _fdatasync(fd: int) -> None:
    if hasattr(os, "fdatasync"):
        os.fdatasync(fd)
    else:
        os.fsync(fd)


def _syncfs(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(os, "syncfs"):
            os.syncfs(fd)
        else:
            os.sync()
    finally:
        os.close(fd)


def _fast_fill_fd(fd: int, size: int, seed: int) -> None:
    block_size = 1024 * 1024
    block = bytes(((seed + i) & 0xFF) for i in range(block_size))
    written = 0
    while written < size:
        data = block[: min(block_size, size - written)]
        n = os.write(fd, data)
        if n <= 0:
            raise OSError("short prefill write")
        written += n


def _write_mmap_pattern(mm: mmap.mmap, offset: int, length: int, config: PatternConfig) -> None:
    pos = 0
    while pos < length:
        n = min(config.chunk_size, length - pos)
        mm[offset + pos:offset + pos + n] = render_pattern_bytes(
            offset + pos,
            n,
            config,
            overlay_off=0,
        )
        pos += n


def _copy_mmap_to_file(mm: mmap.mmap, path: str) -> None:
    with open(path, "wb") as fp:
        pos = 0
        size = len(mm)
        while pos < size:
            n = min(256 * 1024, size - pos)
            fp.write(mm[pos:pos + n])
            pos += n


def _stat_metadata(path: str) -> dict:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"exists": False}
    except Exception as exc:
        return {
            "exists": None,
            "stat_error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "exists": True,
        "size": st.st_size,
        "blocks": st.st_blocks,
        "mode": st.st_mode,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "ino": st.st_ino,
    }


def _seek_offset(fd: int, offset: int, whence: int) -> Optional[int]:
    try:
        return os.lseek(fd, offset, whence)
    except (AttributeError, OSError):
        return None


def _fd_snapshot(fd: int) -> dict:
    st = os.fstat(fd)
    snapshot = {
        "size": st.st_size,
        "blocks": st.st_blocks,
        "ino": st.st_ino,
    }
    if st.st_size > 0:
        hole = _seek_offset(fd, 0, os.SEEK_HOLE)
        data = _seek_offset(fd, 0, os.SEEK_DATA)
        if hole is not None:
            snapshot["seek_hole_0"] = hole
        if data is not None:
            snapshot["seek_data_0"] = data
    return snapshot


def _append_stage_log(trace_path: str, stage: str, path: str, *, fd: Optional[int] = None, **extra: object) -> None:
    record = {
        "time": time.time(),
        "pid": os.getpid(),
        "stage": stage,
        "path": path,
    }
    if fd is not None:
        record["fd_stat"] = _fd_snapshot(fd)
    else:
        record["path_stat"] = _stat_metadata(path)
    if extra:
        record.update(extra)
    line = json.dumps(record, sort_keys=True) + "\n"
    log_fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
    try:
        os.write(log_fd, line.encode("utf-8"))
        os.fsync(log_fd)
    finally:
        os.close(log_fd)


def _save_exception_evidence(
    label: str,
    cfg: ArtifactPressureConfig,
    exc: BaseException,
    paths: dict[str, str],
) -> None:
    fail_dir = os.path.join(cfg.evidence_dir, label)
    ensure_dir(fail_dir)

    stats = {}
    copies = {}
    copy_errors = {}
    for name, path in paths.items():
        stats[name] = _stat_metadata(path)
        if not stats[name].get("exists"):
            continue
        dst = os.path.join(fail_dir, f"{name}.bin")
        try:
            shutil.copyfile(path, dst)
            copies[name] = dst
        except Exception as copy_exc:
            copy_errors[name] = f"{type(copy_exc).__name__}: {copy_exc}"

    meta = {
        "failure": "exception",
        "exception_type": type(exc).__name__,
        "exception": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        "paths": paths,
        "path_stats": stats,
        "copies": copies,
        "copy_errors": copy_errors,
        "time": time.time(),
    }
    with open(os.path.join(fail_dir, "exception_meta.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2, sort_keys=True)


def _save_process_failure_evidence(
    label: str,
    cfg: ArtifactPressureConfig,
    exc: ArtifactWriteProcessError,
    paths: dict[str, str],
) -> None:
    fail_dir = os.path.join(cfg.evidence_dir, label)
    ensure_dir(fail_dir)

    stats = {}
    copies = {}
    copy_errors = {}
    for name, path in paths.items():
        stats[name] = _stat_metadata(path)
        if not stats[name].get("exists"):
            continue
        dst = os.path.join(fail_dir, os.path.basename(path))
        try:
            shutil.copyfile(path, dst)
            copies[name] = dst
        except Exception as copy_exc:
            copy_errors[name] = f"{type(copy_exc).__name__}: {copy_exc}"

    stage_stat = _stat_metadata(exc.trace_path)
    if stage_stat.get("exists"):
        stage_dst = os.path.join(fail_dir, os.path.basename(exc.trace_path))
        try:
            shutil.copyfile(exc.trace_path, stage_dst)
            copies["trace"] = stage_dst
        except Exception as copy_exc:
            copy_errors["trace"] = f"{type(copy_exc).__name__}: {copy_exc}"

    meta = {
        "failure": "write_process",
        "message": str(exc),
        "status": exc.status,
        "exit_code": exc.exit_code,
        "signal_num": exc.signal_num,
        "trace_path": exc.trace_path,
        "trace_stat": stage_stat,
        "paths": paths,
        "path_stats": stats,
        "copies": copies,
        "copy_errors": copy_errors,
        "time": time.time(),
    }
    with open(os.path.join(fail_dir, "process_meta.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2, sort_keys=True)


def _verify_mmap(mm: mmap.mmap, recipe: ArtifactRecipe, label: str, evidence_dir: str) -> bool:
    if len(mm) != recipe.expected_size:
        return False
    for ov in recipe.overlays:
        if ov.offset == 0:
            header = render_pattern_bytes(0, min(PAGE_SIZE, recipe.expected_size), ov.config, ov.overlay_off)
            if mm[:len(header)] != header:
                fail_dir = os.path.join(evidence_dir, label)
                ensure_dir(fail_dir)
                _copy_mmap_to_file(mm, os.path.join(fail_dir, "held_mmap.actual.bin"))
                return False
            break
    return True


def _target_used_percent(path: str) -> float:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    if total <= 0:
        return 0.0
    return 100.0 * (total - avail) / total


def _find_f2fs_mount_root(path: str) -> str:
    real_path = os.path.realpath(path)
    best = ""
    with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as fp:
        for line in fp:
            parts = line.split()
            if len(parts) < 3 or parts[2] != "f2fs":
                continue
            mountpoint = os.path.realpath(parts[1])
            if real_path == mountpoint or real_path.startswith(mountpoint.rstrip("/") + "/"):
                if len(mountpoint) > len(best):
                    best = mountpoint
    return best or path


def _stable_percent(*values: int) -> int:
    acc = 0x345678
    for value in values:
        acc = (acc * 1103515245 + value * 2654435761 + 12345) & 0x7FFFFFFF
    return acc % 100


def should_enable_verity(
    *,
    verity_candidate: bool,
    sync_passes: int,
    verity_min_fsync_passes: int,
) -> bool:
    if not verity_candidate:
        return False
    return sync_passes >= verity_min_fsync_passes


def _is_verity_candidate(seed: int, app: int, artifact: int, verity_ratio_percent: int) -> bool:
    if verity_ratio_percent <= 0:
        return False
    if verity_ratio_percent >= 100:
        return True
    return _stable_percent(seed, app, artifact) < verity_ratio_percent


def _artifact_filename(artifact: int, *, verity_candidate: bool, verity_name_tag: str) -> str:
    if verity_candidate and verity_name_tag:
        return f"artifact_{artifact:02d}.{verity_name_tag}.bin"
    return f"artifact_{artifact:02d}.bin"


def advance_verity_fsync_batch(
    *,
    state: dict[str, ArtifactState],
    paths: tuple[str, ...],
    verity_min_fsync_passes: int,
) -> tuple[str, ...]:
    eligible: list[str] = []
    for path in paths:
        current = state.get(path)
        if current is None:
            continue
        current.sync_passes += 1
        if current.verity_enabled:
            continue
        if should_enable_verity(
            verity_candidate=current.verity_candidate,
            sync_passes=current.sync_passes,
            verity_min_fsync_passes=verity_min_fsync_passes,
        ):
            eligible.append(path)
    return tuple(eligible)


def plan_fsync_batch(
    *,
    focus_path: str,
    candidate_paths: list[str],
    worker_count: int,
    batch_width: int,
    round_id: int,
) -> tuple[str, ...]:
    if worker_count <= 0:
        return ()

    unique = []
    seen = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)

    others = [path for path in unique if path != focus_path]
    if others:
        shift = round_id % len(others)
        others = others[shift:] + others[:shift]

    width = batch_width if batch_width > 0 else worker_count
    batch = [focus_path]
    batch.extend(others[: max(0, width - 1)])
    return tuple(batch[:worker_count])


def _fsync_path(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fsync(fd)
    finally:
        os.close(fd)


def _run_fsync_batch(paths: tuple[str, ...]) -> int:
    if not paths:
        return 0
    with ThreadPoolExecutor(max_workers=len(paths)) as executor:
        futures = [executor.submit(_fsync_path, path) for path in paths]
        for future in futures:
            future.result()
    return len(paths)


def _prefill(enc_root: str, cfg: ArtifactPressureConfig) -> int:
    prefill_dir = os.path.join(enc_root, "prefill")
    ensure_dir(prefill_dir)
    made = 0
    while _target_used_percent(enc_root) < cfg.prefill_percent:
        path = os.path.join(prefill_dir, f"prefill_{made:05d}.bin")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        try:
            os.ftruncate(fd, cfg.prefill_file_bytes)
            _fast_fill_fd(fd, cfg.prefill_file_bytes, cfg.seed ^ 0x505246 ^ made)
            _fdatasync(fd)
        except OSError:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            break
        finally:
            os.close(fd)
        made += 1
    _fsync_dir(prefill_dir)
    return made


def _build_artifact_plan(group: int, app: int, artifact: int, size: int, seed: int) -> ArtifactPlan:
    body_cfg = _mod_cfg(seed ^ (group * 977) ^ (app * 131) ^ artifact)
    header_cfg = _mod_cfg(seed ^ 0x484452 ^ group ^ (app << 8) ^ artifact, chunk_size=PAGE_SIZE)
    extra_cfg = _mod_cfg(seed ^ 0x455854 ^ group ^ (artifact << 4))

    aligned_size = ((size + PAGE_SIZE - 1) // PAGE_SIZE) * PAGE_SIZE
    final_size = aligned_size + ((group + app + artifact) % 3) * 257

    overlays = [OverlaySpec(offset=0, length=min(PAGE_SIZE, final_size), config=header_cfg)]
    if final_size > aligned_size:
        overlays.append(
            OverlaySpec(
                offset=aligned_size,
                length=final_size - aligned_size,
                config=extra_cfg,
                overlay_off=aligned_size,
            )
        )
    return ArtifactPlan(
        recipe=ArtifactRecipe(expected_size=final_size, baseline=body_cfg, overlays=tuple(overlays)),
        body_cfg=body_cfg,
        header_cfg=header_cfg,
        extra_cfg=extra_cfg,
        aligned_size=aligned_size,
        final_size=final_size,
    )


def _write_artifact_tmp_once(path: str, plan: ArtifactPlan, trace_path: str) -> None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        _append_stage_log(trace_path, "open", path, fd=fd, aligned_size=plan.aligned_size, final_size=plan.final_size)
        os.ftruncate(fd, plan.aligned_size)
        _append_stage_log(trace_path, "ftruncate_aligned", path, fd=fd, mapped_len=plan.aligned_size)
        mm = mmap.mmap(fd, plan.aligned_size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            _append_stage_log(trace_path, "mmap_body", path, fd=fd, mapped_len=plan.aligned_size)
            _write_mmap_pattern(mm, 0, plan.aligned_size, plan.body_cfg)
            _append_stage_log(trace_path, "body_write_done", path, fd=fd, mapped_len=plan.aligned_size)
            mm.flush()
            _append_stage_log(trace_path, "body_flush_done", path, fd=fd, mapped_len=plan.aligned_size)
            os.ftruncate(fd, plan.final_size)
            _append_stage_log(trace_path, "ftruncate_final", path, fd=fd, mapped_len=plan.final_size)
            if plan.final_size > plan.aligned_size:
                mm.close()
                _append_stage_log(trace_path, "remap_begin", path, fd=fd, mapped_len=plan.final_size)
                mm = mmap.mmap(fd, plan.final_size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
                _append_stage_log(trace_path, "remap_done", path, fd=fd, mapped_len=plan.final_size)
                _write_mmap_pattern(mm, plan.aligned_size, plan.final_size - plan.aligned_size, plan.extra_cfg)
                _append_stage_log(trace_path, "extra_write_done", path, fd=fd, mapped_len=plan.final_size)
                mm.flush()
                _append_stage_log(trace_path, "extra_flush_done", path, fd=fd, mapped_len=plan.final_size)
            header_len = min(PAGE_SIZE, plan.final_size)
            _append_stage_log(trace_path, "header_store_begin", path, fd=fd, header_len=header_len)
            mm[:header_len] = render_pattern_bytes(0, header_len, plan.header_cfg, overlay_off=0)
            _append_stage_log(trace_path, "header_store_done", path, fd=fd, header_len=header_len)
            _append_stage_log(trace_path, "header_flush_begin", path, fd=fd, header_len=header_len)
            mm.flush(0, header_len)
            _append_stage_log(trace_path, "header_flush_done", path, fd=fd, header_len=header_len)
        finally:
            mm.close()
            _append_stage_log(trace_path, "mmap_closed", path, fd=fd)
        _fdatasync(fd)
        _append_stage_log(trace_path, "fdatasync_done", path, fd=fd)
    finally:
        os.close(fd)
        _append_stage_log(trace_path, "fd_closed", path)


def _write_artifact_tmp(path: str, group: int, app: int, artifact: int, size: int, seed: int) -> ArtifactRecipe:
    plan = _build_artifact_plan(group, app, artifact, size, seed)
    trace_path = path + ".stages.jsonl"
    try:
        os.unlink(trace_path)
    except FileNotFoundError:
        pass

    pid = os.fork()
    if pid == 0:
        rc = 0
        try:
            _write_artifact_tmp_once(path, plan, trace_path)
        except BaseException as exc:
            rc = 101
            _append_stage_log(
                trace_path,
                "child_exception",
                path,
                exception_type=type(exc).__name__,
                exception=str(exc),
            )
        os._exit(rc)

    _, status = os.waitpid(pid, 0)
    if status == 0:
        try:
            os.unlink(trace_path)
        except FileNotFoundError:
            pass
        return plan.recipe
    raise ArtifactWriteProcessError(path, trace_path, status)


def _verify_path(path: str, recipe: ArtifactRecipe, label: str, cfg: ArtifactPressureConfig) -> bool:
    return verify_file_overlays(
        path,
        expected_size=recipe.expected_size,
        baseline=recipe.baseline,
        overlays=recipe.overlays,
        chunk_size=recipe.baseline.chunk_size,
        cold_read=True,
        evidence=VerifyEvidenceConfig(
            out_dir=cfg.evidence_dir,
            label=label,
            save_actual_file=True,
            save_expected_file=cfg.save_expected_file,
            mismatch_window_bytes=4096,
        ),
    )


def run_inline_artifact_pressure(enc_root: str, cfg: ArtifactPressureConfig) -> ArtifactPressureSummary:
    rng = random.Random(cfg.seed)
    mount_root = _find_f2fs_mount_root(enc_root)
    work_dir = os.path.join(enc_root, "inline_artifact_pressure")
    apps_dir = os.path.join(work_dir, "apps")
    churn_dir = os.path.join(work_dir, "churn")
    ensure_dir(apps_dir)
    ensure_dir(churn_dir)
    ensure_dir(cfg.evidence_dir)

    prefill_count = _prefill(enc_root, cfg)
    print(f"[prepare] prefill_files={prefill_count} used={_target_used_percent(enc_root):.1f}%", flush=True)

    churn = ChurnThread(
        churn_dir=churn_dir,
        inline_mode=False,
        file_size=cfg.churn_file_bytes,
        files_per_round=cfg.churn_files_per_round,
        keep_fraction=cfg.churn_keep_fraction,
        interval_s=cfg.churn_interval_s,
        seed=cfg.seed ^ 0x43485552,
        verbose=True,
    )
    gc_thr = GcPulseThread(mountpoint=mount_root, interval_s=cfg.gc_interval_s, verbose=True)
    pressure: Optional[MemoryPressureThread] = None
    if cfg.memory_pressure_bytes > 0:
        pressure = MemoryPressureThread(cfg.memory_pressure_bytes, seed=cfg.seed ^ 0x4D454D)

    state: dict[str, ArtifactState] = {}
    summary = ArtifactPressureSummary(evidence_dir=cfg.evidence_dir)

    churn.start()
    gc_thr.start()
    if pressure is not None:
        pressure.start()

    try:
        for group in range(1, cfg.groups + 1):
            group_start = time.perf_counter()
            app = rng.randrange(cfg.app_count)
            artifact = rng.randrange(cfg.artifacts_per_app)
            verity_candidate = _is_verity_candidate(cfg.seed, app, artifact, cfg.verity_ratio_percent)
            app_dir = os.path.join(apps_dir, f"app_{app:04d}")
            ensure_dir(app_dir)
            final_path = os.path.join(
                app_dir,
                _artifact_filename(
                    artifact,
                    verity_candidate=verity_candidate,
                    verity_name_tag=cfg.verity_name_tag,
                ),
            )
            tmp_path = os.path.join(app_dir, f".artifact_{artifact:02d}.g{group:06d}.tmp")
            backup_path = os.path.join(app_dir, f"artifact_{artifact:02d}.g{group:06d}.backup")
            label = f"group_{group:06d}.app_{app:04d}.artifact_{artifact:02d}"

            held = None
            held_fd = None
            old_state = state.get(final_path)
            old_recipe = old_state.recipe if old_state is not None else None
            if old_state is not None and os.path.exists(final_path):
                held_fd = os.open(final_path, os.O_RDONLY | os.O_CLOEXEC)
                held = mmap.mmap(held_fd, 0, access=mmap.ACCESS_READ)

            try:
                size = rng.randint(cfg.artifact_min_bytes, cfg.artifact_max_bytes)
                recipe = _write_artifact_tmp(tmp_path, group, app, artifact, size, cfg.seed)
                if old_recipe is not None and os.path.exists(final_path):
                    os.rename(final_path, backup_path)
                os.rename(tmp_path, final_path)
                _fsync_dir(app_dir)
                if os.path.exists(backup_path):
                    os.unlink(backup_path)
                    _fsync_dir(app_dir)
            except ArtifactWriteProcessError as exc:
                failure_label = label + ".write_signal"
                _save_process_failure_evidence(
                    failure_label,
                    cfg,
                    exc,
                    {
                        "tmp": tmp_path,
                        "final": final_path,
                        "backup": backup_path,
                    },
                )
                summary.corrupt_label = failure_label
                summary.corrupt_path = tmp_path
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:
                exception_label = label + ".write_exception"
                _save_exception_evidence(
                    exception_label,
                    cfg,
                    exc,
                    {"tmp": tmp_path, "final": final_path, "backup": backup_path},
                )
                summary.corrupt_label = exception_label
                summary.corrupt_path = tmp_path
                raise

            state[final_path] = ArtifactState(
                recipe=recipe,
                verity_candidate=verity_candidate,
            )
            summary.files_written += 1

            if held is not None and old_recipe is not None:
                if not _verify_mmap(held, old_recipe, label + ".held_old_mmap", cfg.evidence_dir):
                    summary.corrupt_label = label + ".held_old_mmap"
                    summary.corrupt_path = final_path
                    return summary
                held.close()
                held = None
            if held_fd is not None:
                os.close(held_fd)
                held_fd = None

            if cfg.verify_every_groups > 0 and group % cfg.verify_every_groups == 0:
                if not _verify_path(final_path, recipe, label, cfg):
                    summary.corrupt_label = label
                    summary.corrupt_path = final_path
                    return summary
                summary.files_verified += 1

            fsync_batch = plan_fsync_batch(
                focus_path=final_path,
                candidate_paths=[path for path in state if os.path.exists(path)],
                worker_count=cfg.fsync_worker_count,
                batch_width=cfg.fsync_batch_width,
                round_id=group,
            )
            if fsync_batch:
                summary.fsync_batches += 1
                summary.fsync_files += _run_fsync_batch(fsync_batch)
                for verity_path in advance_verity_fsync_batch(
                    state=state,
                    paths=fsync_batch,
                    verity_min_fsync_passes=cfg.verity_min_fsync_passes,
                ):
                    enable_fsverity(verity_path)
                    verity_state = state.get(verity_path)
                    if verity_state is None or verity_state.verity_enabled:
                        continue
                    verity_state.verity_enabled = True
                    summary.verity_enabled += 1
                    print(
                        f"[verity] enabled path={verity_path} sync_passes={verity_state.sync_passes}",
                        flush=True,
                    )

            current_state = state[final_path]

            if cfg.sync_every_groups > 0 and group % cfg.sync_every_groups == 0:
                _syncfs(enc_root)
                drop_caches(3)

            if not cfg.keep_success_files and len(state) > cfg.app_count * cfg.artifacts_per_app:
                victim = next(iter(state))
                state.pop(victim, None)
                try:
                    os.unlink(victim)
                except FileNotFoundError:
                    pass

            summary.groups_done = group
            print(
                f"[group {group}] app={app} artifact={artifact} size={recipe.expected_size} "
                f"elapsed={time.perf_counter() - group_start:.3f}s "
                f"fsync={len(fsync_batch) if fsync_batch else 0} "
                f"verity={int(current_state.verity_enabled)} "
                f"verity_total={summary.verity_enabled} "
                f"churn={churn.created}/{churn.deleted} errors={churn.errors} "
                f"gc={gc_thr.success}/{gc_thr.pulses}",
                flush=True,
            )
    finally:
        if pressure is not None:
            pressure.stop()
            pressure.join(timeout=5)
        churn.stop()
        churn.join(timeout=5)
        gc_thr.stop()
        gc_thr.join(timeout=5)

        if not cfg.keep_success_files and not summary.corrupt_label:
            shutil.rmtree(work_dir, ignore_errors=True)

    return summary
