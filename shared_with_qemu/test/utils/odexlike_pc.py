import gc
import json
import mmap
import os
import random
import shutil
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Optional


PAGE_SIZE = 4096
HEADER_STRUCT = struct.Struct("<8s6I")
MAGIC = b"ODXLK001"
HEADER_SIZE = HEADER_STRUCT.size
PAYLOAD_SIZE = PAGE_SIZE - HEADER_SIZE


@dataclass(frozen=True)
class VariantConfig:
    name: str
    writer: str
    generations: int
    page_count: int
    seed: int
    page_sleep_max_ms: int
    phase_sleep_max_ms: int
    consumer_phase_mode: str
    consumer_launch_slot: int
    pressure_mb: int
    flush_each_page: bool
    tail_bytes: int = 0


@dataclass
class VariantResult:
    name: str
    producer_generations: int
    consumer_passes: int
    enoent_retries: int
    mismatches: int
    producer_error: str = ""
    consumer_error: str = ""
    log_path: str = ""
    work_dir: str = ""


@dataclass
class PhaseSnapshot:
    generation: int
    slot: int
    phase: str
    had_old: bool
    seq: int


class PhaseTimeline:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._generation = 0
        self._slot = -1
        self._phase = "init"
        self._had_old = False
        self._seq = 0
        self._producer_done = False

    def publish(self, generation: int, slot: int, phase: str, had_old: bool) -> None:
        with self._cond:
            self._generation = generation
            self._slot = slot
            self._phase = phase
            self._had_old = had_old
            self._seq += 1
            self._cond.notify_all()

    def snapshot(self) -> PhaseSnapshot:
        with self._cond:
            return PhaseSnapshot(
                generation=self._generation,
                slot=self._slot,
                phase=self._phase,
                had_old=self._had_old,
                seq=self._seq,
            )

    def mark_done(self) -> None:
        with self._cond:
            self._producer_done = True
            self._cond.notify_all()

    def wait_for_at_least(self, generation: int, slot: int) -> PhaseSnapshot:
        with self._cond:
            while True:
                if self._generation > generation:
                    return PhaseSnapshot(
                        generation=self._generation,
                        slot=self._slot,
                        phase=self._phase,
                        had_old=self._had_old,
                        seq=self._seq,
                    )
                if self._generation == generation and self._slot >= slot:
                    return PhaseSnapshot(
                        generation=self._generation,
                        slot=self._slot,
                        phase=self._phase,
                        had_old=self._had_old,
                        seq=self._seq,
                    )
                if self._producer_done and self._generation < generation:
                    return PhaseSnapshot(
                        generation=self._generation,
                        slot=self._slot,
                        phase=self._phase,
                        had_old=self._had_old,
                        seq=self._seq,
                    )
                self._cond.wait()


def phase_slot_total(cfg: VariantConfig) -> int:
    return cfg.page_count + 6


def phase_name_for_slot(cfg: VariantConfig, slot: int) -> str:
    if slot <= 0:
        return "gen_start"
    if 1 <= slot <= cfg.page_count:
        return f"page_{slot - 1:04d}_written"
    if slot == cfg.page_count + 1:
        return "fsync_tmp"
    if slot == cfg.page_count + 2:
        return "pre_rename_backup"
    if slot == cfg.page_count + 3:
        return "post_rename_backup"
    if slot == cfg.page_count + 4:
        return "post_rename_base"
    return "post_unlink_backup"


def consumer_target_slot(cfg: VariantConfig, generation: int) -> int:
    total = phase_slot_total(cfg)
    start = cfg.consumer_launch_slot % total
    if cfg.consumer_phase_mode == "fixed":
        return start
    if cfg.consumer_phase_mode == "sweep":
        return (start + generation - 1) % total
    raise ValueError(f"unknown consumer_phase_mode: {cfg.consumer_phase_mode}")


class JsonLogger:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._fp = open(path, "w", encoding="utf-8")

    def log(self, role: str, phase: str, **fields: object) -> None:
        rec = {
            "ts": time.time(),
            "role": role,
            "phase": phase,
        }
        rec.update(fields)
        line = json.dumps(rec, sort_keys=True, ensure_ascii=True)
        with self._lock:
            self._fp.write(line + "\n")
            self._fp.flush()

    def close(self) -> None:
        with self._lock:
            self._fp.close()


def _payload_bytes(seed: int, generation: int, page_index: int) -> bytes:
    base = (seed ^ (generation * 131) ^ (page_index * 17)) & 0xFFFFFFFF
    return bytes(((base + i) & 0xFF) for i in range(PAYLOAD_SIZE))


def build_page(seed: int, generation: int, page_index: int) -> bytes:
    payload = _payload_bytes(seed, generation, page_index)
    payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
    header = HEADER_STRUCT.pack(
        MAGIC,
        generation,
        page_index,
        PAYLOAD_SIZE,
        payload_crc,
        0,
        seed & 0xFFFFFFFF,
    )
    header_crc = zlib.crc32(header[:24] + struct.pack("<I", 0) + header[28:]) & 0xFFFFFFFF
    header = HEADER_STRUCT.pack(
        MAGIC,
        generation,
        page_index,
        PAYLOAD_SIZE,
        payload_crc,
        header_crc,
        seed & 0xFFFFFFFF,
    )
    return header + payload


def validate_mapping(mm: mmap.mmap, expected_pages: int, tail_bytes: int = 0) -> tuple[int, int]:
    size = len(mm)
    expected_size = expected_pages * PAGE_SIZE + tail_bytes
    if size != expected_size:
        raise ValueError(f"size mismatch: got={size} expect={expected_size}")

    seen_generation: Optional[int] = None
    for page_index in range(expected_pages):
        start = page_index * PAGE_SIZE
        page = mm[start:start + PAGE_SIZE]
        if len(page) != PAGE_SIZE:
            raise ValueError(f"short mmap slice at page={page_index}")
        magic, generation, got_index, payload_len, payload_crc, header_crc, seed = HEADER_STRUCT.unpack(
            page[:HEADER_SIZE]
        )
        if magic != MAGIC:
            raise ValueError(f"bad magic at page={page_index}: {magic!r}")
        if got_index != page_index:
            raise ValueError(f"page index mismatch at page={page_index}: got={got_index}")
        if payload_len != PAYLOAD_SIZE:
            raise ValueError(f"payload len mismatch at page={page_index}: got={payload_len}")
        calc_header_crc = zlib.crc32(page[:24] + struct.pack("<I", 0) + page[28:HEADER_SIZE]) & 0xFFFFFFFF
        if header_crc != calc_header_crc:
            raise ValueError(
                f"header crc mismatch at page={page_index}: got={header_crc:#x} calc={calc_header_crc:#x}"
            )
        payload = page[HEADER_SIZE:]
        calc_payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
        if payload_crc != calc_payload_crc:
            raise ValueError(
                f"payload crc mismatch at page={page_index}: got={payload_crc:#x} calc={calc_payload_crc:#x}"
            )
        expected_payload = _payload_bytes(seed, generation, page_index)
        if payload != expected_payload:
            raise ValueError(f"payload bytes mismatch at page={page_index} generation={generation}")
        if seen_generation is None:
            seen_generation = generation
        elif seen_generation != generation:
            raise ValueError(
                f"mixed generation: first={seen_generation} page={page_index} got={generation}"
            )

    assert seen_generation is not None
    return seen_generation, expected_pages


def fsync_dir(path: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sleep_jitter(rng: random.Random, upper_ms: int) -> None:
    if upper_ms <= 0:
        return
    time.sleep(rng.uniform(0.0, upper_ms / 1000.0))


def _write_buffered(
    tmp_path: str,
    cfg: VariantConfig,
    generation: int,
    rng: random.Random,
    log: JsonLogger,
    timeline: PhaseTimeline,
    had_old: bool,
) -> None:
    fd = os.open(tmp_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        for page_index in range(cfg.page_count):
            page = build_page(cfg.seed, generation, page_index)
            total = 0
            while total < len(page):
                wrote = os.write(fd, page[total:])
                if wrote <= 0:
                    raise OSError(f"os.write returned {wrote} at page={page_index}")
                if wrote != len(page) - total:
                    log.log(
                        "producer",
                        "short_write",
                        generation=generation,
                        page_index=page_index,
                        wrote=wrote,
                        remaining=len(page) - total,
                    )
                total += wrote
            timeline.publish(generation, 1 + page_index, phase_name_for_slot(cfg, 1 + page_index), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=1 + page_index,
                slot_phase=phase_name_for_slot(cfg, 1 + page_index),
                had_old=had_old,
            )
            _sleep_jitter(rng, cfg.page_sleep_max_ms)
        if cfg.tail_bytes > 0:
            tail = bytes((generation + i) & 0xFF for i in range(cfg.tail_bytes))
            wrote = os.write(fd, tail)
            if wrote != cfg.tail_bytes:
                raise OSError(f"tail write short: wrote={wrote} expected={cfg.tail_bytes}")
            log.log("producer", "tail_write", generation=generation, tail_bytes=cfg.tail_bytes)
        os.fsync(fd)
        timeline.publish(generation, cfg.page_count + 1, phase_name_for_slot(cfg, cfg.page_count + 1), had_old)
        log.log(
            "producer",
            "phase",
            generation=generation,
            slot=cfg.page_count + 1,
            slot_phase=phase_name_for_slot(cfg, cfg.page_count + 1),
            had_old=had_old,
        )
        log.log("producer", "fsync_tmp", generation=generation, path=tmp_path)
    finally:
        os.close(fd)


def _write_mmap(
    tmp_path: str,
    cfg: VariantConfig,
    generation: int,
    rng: random.Random,
    log: JsonLogger,
    timeline: PhaseTimeline,
    had_old: bool,
) -> None:
    size = cfg.page_count * PAGE_SIZE + cfg.tail_bytes
    fd = os.open(tmp_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, size)
        mm = mmap.mmap(fd, size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        try:
            for page_index in range(cfg.page_count):
                start = page_index * PAGE_SIZE
                mm[start:start + PAGE_SIZE] = build_page(cfg.seed, generation, page_index)
                if cfg.flush_each_page:
                    mm.flush(start, PAGE_SIZE)
                    log.log("producer", "flush_page", generation=generation, page_index=page_index)
                timeline.publish(generation, 1 + page_index, phase_name_for_slot(cfg, 1 + page_index), had_old)
                log.log(
                    "producer",
                    "phase",
                    generation=generation,
                    slot=1 + page_index,
                    slot_phase=phase_name_for_slot(cfg, 1 + page_index),
                    had_old=had_old,
                )
                _sleep_jitter(rng, cfg.page_sleep_max_ms)
            if cfg.tail_bytes > 0:
                tail_start = cfg.page_count * PAGE_SIZE
                tail = bytes((generation + i) & 0xFF for i in range(cfg.tail_bytes))
                mm[tail_start:tail_start + cfg.tail_bytes] = tail
                log.log("producer", "tail_write", generation=generation, tail_bytes=cfg.tail_bytes)
            mm.flush()
        finally:
            mm.close()
        os.fsync(fd)
        timeline.publish(generation, cfg.page_count + 1, phase_name_for_slot(cfg, cfg.page_count + 1), had_old)
        log.log(
            "producer",
            "phase",
            generation=generation,
            slot=cfg.page_count + 1,
            slot_phase=phase_name_for_slot(cfg, cfg.page_count + 1),
            had_old=had_old,
        )
        log.log("producer", "fsync_tmp", generation=generation, path=tmp_path)
    finally:
        os.close(fd)


def _pressure_worker(stop_event: threading.Event, bytes_target: int, seed: int, log: JsonLogger) -> None:
    rng = random.Random(seed)
    chunks: list[bytearray] = []
    chunk_bytes = 8 * 1024 * 1024
    touched = 0
    try:
        while not stop_event.is_set():
            while touched < bytes_target and not stop_event.is_set():
                buf = bytearray(chunk_bytes)
                for off in range(0, len(buf), PAGE_SIZE):
                    buf[off] = (off // PAGE_SIZE + rng.randint(0, 255)) & 0xFF
                chunks.append(buf)
                touched += len(buf)
            rng.shuffle(chunks)
            for buf in chunks[: max(1, len(chunks) // 4)]:
                for off in range(0, len(buf), PAGE_SIZE * 8):
                    buf[off] = (buf[off] + 1) & 0xFF
            time.sleep(0.02)
            if len(chunks) > 4:
                del chunks[: len(chunks) // 3]
                gc.collect()
                touched = sum(len(x) for x in chunks)
    except MemoryError:
        log.log("pressure", "memory_error", bytes_target=bytes_target)
    finally:
        chunks.clear()
        gc.collect()


def _producer(
    root_dir: str,
    base_path: str,
    cfg: VariantConfig,
    timeline: PhaseTimeline,
    stop_event: threading.Event,
    producer_done: threading.Event,
    result: VariantResult,
    log: JsonLogger,
) -> None:
    rng = random.Random(cfg.seed ^ 0x50524F44)
    writer = _write_buffered if cfg.writer == "buffered" else _write_mmap

    try:
        for generation in range(1, cfg.generations + 1):
            if stop_event.is_set():
                break
            tmp_path = os.path.join(root_dir, f"{cfg.name}.g{generation:04d}.tmp")
            backup_path = os.path.join(root_dir, f"{cfg.name}.g{generation:04d}.backup")
            had_old = os.path.exists(base_path)
            timeline.publish(generation, 0, phase_name_for_slot(cfg, 0), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=0,
                slot_phase=phase_name_for_slot(cfg, 0),
                had_old=had_old,
            )
            writer(tmp_path, cfg, generation, rng, log, timeline, had_old)
            fsync_dir(root_dir)
            _sleep_jitter(rng, cfg.phase_sleep_max_ms)

            timeline.publish(generation, cfg.page_count + 2, phase_name_for_slot(cfg, cfg.page_count + 2), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=cfg.page_count + 2,
                slot_phase=phase_name_for_slot(cfg, cfg.page_count + 2),
                had_old=had_old,
            )
            if had_old:
                os.rename(base_path, backup_path)
                log.log("producer", "rename_backup", generation=generation, backup_path=backup_path)
                _sleep_jitter(rng, cfg.phase_sleep_max_ms)
            timeline.publish(generation, cfg.page_count + 3, phase_name_for_slot(cfg, cfg.page_count + 3), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=cfg.page_count + 3,
                slot_phase=phase_name_for_slot(cfg, cfg.page_count + 3),
                had_old=had_old,
            )

            os.rename(tmp_path, base_path)
            log.log("producer", "rename_base", generation=generation, base_path=base_path)
            fsync_dir(root_dir)
            timeline.publish(generation, cfg.page_count + 4, phase_name_for_slot(cfg, cfg.page_count + 4), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=cfg.page_count + 4,
                slot_phase=phase_name_for_slot(cfg, cfg.page_count + 4),
                had_old=had_old,
            )
            _sleep_jitter(rng, cfg.phase_sleep_max_ms)

            if had_old and os.path.exists(backup_path):
                os.unlink(backup_path)
                log.log("producer", "unlink_backup", generation=generation, backup_path=backup_path)
                fsync_dir(root_dir)
            timeline.publish(generation, cfg.page_count + 5, phase_name_for_slot(cfg, cfg.page_count + 5), had_old)
            log.log(
                "producer",
                "phase",
                generation=generation,
                slot=cfg.page_count + 5,
                slot_phase=phase_name_for_slot(cfg, cfg.page_count + 5),
                had_old=had_old,
            )

            result.producer_generations = generation
            _sleep_jitter(rng, cfg.phase_sleep_max_ms)
    except Exception as exc:
        result.producer_error = str(exc)
        stop_event.set()
        log.log("producer", "error", error=str(exc))
    finally:
        timeline.mark_done()
        producer_done.set()


def _consumer(
    base_path: str,
    cfg: VariantConfig,
    timeline: PhaseTimeline,
    stop_event: threading.Event,
    producer_done: threading.Event,
    result: VariantResult,
    log: JsonLogger,
) -> None:
    snapshot_dir = os.path.join(os.path.dirname(base_path), "snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    terminal_slot = phase_slot_total(cfg) - 1

    for generation in range(1, cfg.generations + 1):
        if stop_event.is_set():
            return
        target_slot = consumer_target_slot(cfg, generation)
        target_phase = phase_name_for_slot(cfg, target_slot)
        log.log(
            "consumer",
            "wait_target",
            target_generation=generation,
            target_slot=target_slot,
            target_phase=target_phase,
            mode=cfg.consumer_phase_mode,
        )
        snapshot = timeline.wait_for_at_least(generation, target_slot)
        if snapshot.generation < generation and producer_done.is_set():
            return
        if snapshot.generation > generation:
            log.log(
                "consumer",
                "missed_target",
                target_generation=generation,
                target_slot=target_slot,
                target_phase=target_phase,
                seen_generation=snapshot.generation,
                seen_slot=snapshot.slot,
                seen_phase=snapshot.phase,
            )
            continue

        try:
            fd = os.open(base_path, os.O_RDONLY)
        except FileNotFoundError:
            result.enoent_retries += 1
            log.log(
                "consumer",
                "enoent",
                path=base_path,
                target_generation=generation,
                target_slot=target_slot,
                target_phase=target_phase,
                seen_generation=snapshot.generation,
                seen_slot=snapshot.slot,
                seen_phase=snapshot.phase,
            )
            timeline.wait_for_at_least(generation, terminal_slot)
            continue

        mm = None
        try:
            st = os.fstat(fd)
            mm = mmap.mmap(fd, st.st_size, access=mmap.ACCESS_READ)
            mapped_generation, pages = validate_mapping(mm, cfg.page_count, cfg.tail_bytes)
            result.consumer_passes += 1
            log.log(
                "consumer",
                "validate_ok",
                generation=mapped_generation,
                pages=pages,
                inode=st.st_ino,
                size=st.st_size,
                target_generation=generation,
                target_slot=target_slot,
                target_phase=target_phase,
                seen_generation=snapshot.generation,
                seen_slot=snapshot.slot,
                seen_phase=snapshot.phase,
            )
            timeline.wait_for_at_least(generation, terminal_slot)
            held_generation, held_pages = validate_mapping(mm, cfg.page_count, cfg.tail_bytes)
            result.consumer_passes += 1
            log.log(
                "consumer",
                "hold_validate_ok",
                generation=held_generation,
                pages=held_pages,
                inode=st.st_ino,
                size=st.st_size,
                target_generation=generation,
                target_slot=target_slot,
                target_phase=target_phase,
            )
        except Exception as exc:
            result.mismatches += 1
            result.consumer_error = str(exc)
            stamp = int(time.time() * 1000)
            snap_path = os.path.join(snapshot_dir, f"{cfg.name}.corrupt.{stamp}.bin")
            try:
                shutil.copy2(base_path, snap_path)
            except Exception:
                snap_path = ""
            log.log("consumer", "validate_fail", error=str(exc), snapshot=snap_path)
            stop_event.set()
            return
        finally:
            if mm is not None:
                mm.close()
            os.close(fd)


def run_variant(work_root: str, cfg: VariantConfig) -> VariantResult:
    variant_dir = os.path.join(work_root, cfg.name)
    if os.path.exists(variant_dir):
        shutil.rmtree(variant_dir)
    os.makedirs(variant_dir, exist_ok=True)
    base_path = os.path.join(variant_dir, "base.odexlike")
    log_path = os.path.join(variant_dir, "events.jsonl")
    result = VariantResult(name=cfg.name, producer_generations=0, consumer_passes=0, enoent_retries=0, mismatches=0)
    result.log_path = log_path
    result.work_dir = variant_dir

    log = JsonLogger(log_path)
    timeline = PhaseTimeline()
    stop_event = threading.Event()
    producer_done = threading.Event()
    threads = [
        threading.Thread(
            target=_producer,
            name=f"{cfg.name}-producer",
            args=(variant_dir, base_path, cfg, timeline, stop_event, producer_done, result, log),
            daemon=True,
        ),
        threading.Thread(
            target=_consumer,
            name=f"{cfg.name}-consumer",
            args=(base_path, cfg, timeline, stop_event, producer_done, result, log),
            daemon=True,
        ),
    ]

    if cfg.pressure_mb > 0:
        threads.append(
            threading.Thread(
                target=_pressure_worker,
                name=f"{cfg.name}-pressure",
                args=(stop_event, cfg.pressure_mb * 1024 * 1024, cfg.seed ^ 0x50524553, log),
                daemon=True,
            )
        )

    start = time.time()
    log.log(
        "main",
        "variant_start",
        variant=cfg.name,
        writer=cfg.writer,
        seed=cfg.seed,
        consumer_phase_mode=cfg.consumer_phase_mode,
        consumer_launch_slot=cfg.consumer_launch_slot,
        phase_slot_total=phase_slot_total(cfg),
    )
    for th in threads:
        th.start()
    for th in threads[:2]:
        th.join()
    stop_event.set()
    for th in threads[2:]:
        th.join(timeout=1.0)
    elapsed = time.time() - start
    log.log(
        "main",
        "variant_done",
        variant=cfg.name,
        elapsed_sec=elapsed,
        producer_generations=result.producer_generations,
        consumer_passes=result.consumer_passes,
        enoent_retries=result.enoent_retries,
        mismatches=result.mismatches,
        producer_error=result.producer_error,
        consumer_error=result.consumer_error,
    )
    log.close()
    return result
