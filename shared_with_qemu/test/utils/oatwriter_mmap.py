import ctypes
import mmap
import os
import struct
import time
from dataclasses import dataclass

from .io import ensure_dir
from .memory_pressure import MemoryPressureThread
from .sysutil import drop_caches
from .verify import hex_dump


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
MS_SYNC = 4
HEADER_MAGIC = b"VDXST001"
SECTION_MAGIC = b"SECT"
HEADER_STRUCT = struct.Struct("<8s8I")
SECTION_ENTRY_STRUCT = struct.Struct("<4sIII")
LIBC = ctypes.CDLL(None, use_errno=True)


def _round_up(value: int, align: int) -> int:
    if align <= 0:
        raise ValueError("align must be > 0")
    return ((value + align - 1) // align) * align


def _mapped_addr(mm: mmap.mmap) -> int:
    return ctypes.addressof(ctypes.c_char.from_buffer(mm))


def _memcpy(mm: mmap.mmap, offset: int, data: bytes) -> None:
    if offset < 0 or offset + len(data) > len(mm):
        raise ValueError(f"memcpy out of range offset={offset} len={len(data)} map_len={len(mm)}")
    ctypes.memmove(_mapped_addr(mm) + offset, data, len(data))


def _msync(mm: mmap.mmap, length: int) -> None:
    if length <= 0:
        return
    rc = LIBC.msync(ctypes.c_void_p(_mapped_addr(mm)), ctypes.c_size_t(length), ctypes.c_int(MS_SYNC))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"msync failed len={length}: {os.strerror(err)}")


def _assert_bytes_equal(actual: bytes, expected: bytes, context: str) -> None:
    if actual == expected:
        return
    mismatch = next((i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]), None)
    if mismatch is None:
        raise AssertionError(f"{context}: length mismatch actual={len(actual)} expected={len(expected)}")
    lo = max(0, mismatch - 64)
    hi = min(min(len(actual), len(expected)), mismatch + 64)
    raise AssertionError(
        f"{context}: mismatch at offset {mismatch}\n"
        f"expected:\n{hex_dump(expected[lo:hi], base=lo)}\n"
        f"actual:\n{hex_dump(actual[lo:hi], base=lo)}"
    )


def _build_bytes(length: int, seed: int, tag: int) -> bytes:
    out = bytearray(length)
    for i in range(length):
        out[i] = (seed + tag * 29 + i * 17 + (i >> 7)) & 0xFF
    return bytes(out)


def _build_section_table(group_index: int, file_index: int, blob_count: int, seed: int) -> bytes:
    entries = bytearray()
    for blob_index in range(blob_count):
        entries.extend(
            SECTION_ENTRY_STRUCT.pack(
                SECTION_MAGIC,
                group_index,
                file_index,
                (seed + blob_index * 131) & 0xFFFFFFFF,
            )
        )
    return bytes(entries)


def _build_header(
    *,
    total_size: int,
    old_size: int,
    page_aligned_size: int,
    blob_count: int,
    extra_size: int,
    seed: int,
    group_index: int,
    file_index: int,
) -> bytes:
    header = HEADER_STRUCT.pack(
        HEADER_MAGIC,
        total_size,
        old_size,
        page_aligned_size,
        blob_count,
        extra_size,
        seed,
        group_index,
        file_index,
    )
    return header + b"\x00" * (PAGE_SIZE - len(header))


def _read_cold(path: str, *, drop_first: bool) -> bytes:
    if drop_first:
        drop_caches(3)
    with open(path, "rb") as fp:
        return fp.read()


@dataclass(frozen=True)
class OatWriterStressConfig:
    groups: int
    files_per_group: int
    dex_blob_count: int
    dex_blob_size: int
    extra_buffer_size: int
    pressure_bytes: int
    seed: int
    verify_drop_caches: bool
    keep_files: bool


@dataclass(frozen=True)
class OatWriterStressSummary:
    files_written: int
    files_verified: int
    total_verified_bytes: int
    peak_file_size: int


def write_oatwriter_like_file(
    path: str,
    *,
    group_index: int,
    file_index: int,
    config: OatWriterStressConfig,
) -> bytes:
    ensure_dir(os.path.dirname(path))
    seed = config.seed + group_index * 977 + file_index * 131
    section_table = _build_section_table(group_index, file_index, config.dex_blob_count, seed)

    vdex_size = PAGE_SIZE + len(section_table)
    dex_offsets: list[int] = []
    dex_payloads: list[bytes] = []
    for blob_index in range(config.dex_blob_count):
        vdex_size = _round_up(vdex_size, 4)
        blob_size = config.dex_blob_size + blob_index * 4096 + ((group_index + file_index + blob_index) % 3) * 257
        dex_offsets.append(vdex_size)
        dex_payloads.append(_build_bytes(blob_size, seed, blob_index + 1))
        vdex_size += blob_size

    old_vdex_size = vdex_size
    page_aligned_size = _round_up(old_vdex_size, PAGE_SIZE)
    extra_buffer = _build_bytes(config.extra_buffer_size + (group_index % 3) * 73, seed, 99)
    final_size = old_vdex_size + len(extra_buffer)
    header = _build_header(
        total_size=final_size,
        old_size=old_vdex_size,
        page_aligned_size=page_aligned_size,
        blob_count=config.dex_blob_count,
        extra_size=len(extra_buffer),
        seed=seed,
        group_index=group_index,
        file_index=file_index,
    )

    expected = bytearray(final_size)
    expected[:PAGE_SIZE] = header
    expected[PAGE_SIZE:PAGE_SIZE + len(section_table)] = section_table
    for offset, payload in zip(dex_offsets, dex_payloads):
        expected[offset:offset + len(payload)] = payload
    expected[old_vdex_size:old_vdex_size + len(extra_buffer)] = extra_buffer

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
    try:
        os.ftruncate(fd, page_aligned_size)
        base_map = mmap.mmap(fd, page_aligned_size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        extra_map = None
        try:
            _memcpy(base_map, PAGE_SIZE, section_table)
            for offset, payload in zip(dex_offsets, dex_payloads):
                _memcpy(base_map, offset, payload)

            os.ftruncate(fd, final_size)
            mmapped_old_size = _round_up(old_vdex_size, PAGE_SIZE)
            first_chunk_size = min(len(extra_buffer), mmapped_old_size - old_vdex_size)
            if first_chunk_size:
                _memcpy(base_map, old_vdex_size, extra_buffer[:first_chunk_size])

            if first_chunk_size != len(extra_buffer):
                tail_size = len(extra_buffer) - first_chunk_size
                extra_map = mmap.mmap(
                    fd,
                    tail_size,
                    flags=mmap.MAP_SHARED,
                    prot=mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=mmapped_old_size,
                )
                _memcpy(extra_map, 0, extra_buffer[first_chunk_size:])

            _msync(base_map, _round_up(old_vdex_size, PAGE_SIZE))
            if extra_map is not None:
                _msync(extra_map, len(extra_buffer) - first_chunk_size)

            _memcpy(base_map, 0, header)
            _msync(base_map, PAGE_SIZE)
        finally:
            if extra_map is not None:
                extra_map.close()
            base_map.close()
    finally:
        os.close(fd)

    return bytes(expected)


def run_oatwriter_mmap_stress(root_dir: str, config: OatWriterStressConfig) -> OatWriterStressSummary:
    work_dir = os.path.join(root_dir, "oatwriter_mmap_stress")
    ensure_dir(work_dir)

    pressure = None
    if config.pressure_bytes > 0:
        pressure = MemoryPressureThread(config.pressure_bytes, seed=config.seed ^ 0xA57E551)
        pressure.start()

    files_written = 0
    files_verified = 0
    total_verified_bytes = 0
    peak_file_size = 0
    started = time.perf_counter()

    try:
        for group_index in range(config.groups):
            group_start = time.perf_counter()
            group_dir = os.path.join(work_dir, f"group_{group_index:04d}")
            ensure_dir(group_dir)
            for file_index in range(config.files_per_group):
                path = os.path.join(group_dir, f"vdex_like_{file_index:02d}.bin")
                expected = write_oatwriter_like_file(
                    path,
                    group_index=group_index,
                    file_index=file_index,
                    config=config,
                )
                actual = _read_cold(path, drop_first=config.verify_drop_caches)
                _assert_bytes_equal(actual, expected, f"group={group_index} file={file_index}")
                files_written += 1
                files_verified += 1
                total_verified_bytes += len(actual)
                peak_file_size = max(peak_file_size, len(actual))
                if not config.keep_files:
                    os.unlink(path)
            if not config.keep_files:
                os.rmdir(group_dir)
            print(
                f"[group {group_index}] files={config.files_per_group} "
                f"elapsed={time.perf_counter() - group_start:.3f}s",
                flush=True,
            )
    finally:
        if pressure is not None:
            pressure.stop()
            pressure.join(timeout=5)

    print(f"[summary] total_elapsed={time.perf_counter() - started:.3f}s", flush=True)
    return OatWriterStressSummary(
        files_written=files_written,
        files_verified=files_verified,
        total_verified_bytes=total_verified_bytes,
        peak_file_size=peak_file_size,
    )
