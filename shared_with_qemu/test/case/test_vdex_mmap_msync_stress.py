#!/usr/bin/env python3
"""
Stress test reproducing Android ART vdex write pattern (oat_writer.cc):
  ftruncate → mmap(MAP_SHARED) → memcpy body (skip header) →
  msync body → write header last → msync first page

Runs CONCURRENCY files in parallel, looping forever (or LOOPS times).
Deletes all files at the start of each loop.
"""
import mmap
import os
import struct
import sys
import threading
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

# === CONFIG ===
WORK_DIR = "/mnt/f2fs/vdex_stress"
CONCURRENCY = 32
LOOPS = 0  # 0 = infinite
NUM_DEX_PER_VDEX = 2
DEX_SIZE = 2 * 1024 * 1024  # 2MB per dex
VERIFIER_DEPS_SIZE = 32 * 1024
LOOKUP_TABLE_SIZE = 16 * 1024
PAGE_SIZE = 4096
SEED = 0xA270

# vdex header constants (match AOSP vdex_file.h)
VDEX_MAGIC = b"vdex"
VDEX_VERSION = b"027\x00"
VDEX_INVALID_MAGIC = b"wdex"
NUM_SECTIONS = 4
SIZEOF_HEADER = 12  # magic(4) + version(4) + num_sections(4)
SIZEOF_SECTION_HDR = 12  # kind(4) + offset(4) + size(4)
SIZEOF_CHECKSUM = 4


def round_up(val, align):
    return (val + align - 1) & ~(align - 1)


def make_dex_content(file_idx, dex_idx, size):
    token = ((file_idx * 7 + dex_idx * 13 + SEED) & 0xFF).to_bytes(1, "little")
    return (token * size)[:size]


def make_filler(label_byte, size):
    return (bytes([label_byte]) * size)[:size]


def write_one_vdex(path, file_idx):
    D = NUM_DEX_PER_VDEX
    header_area_size = SIZEOF_HEADER + NUM_SECTIONS * SIZEOF_SECTION_HDR + D * SIZEOF_CHECKSUM
    dex_bodies = [make_dex_content(file_idx, i, DEX_SIZE) for i in range(D)]

    vdex_size = header_area_size
    dex_offsets = []
    for i in range(D):
        vdex_size = round_up(vdex_size, 4)
        dex_offsets.append(vdex_size)
        vdex_size += DEX_SIZE

    old_vdex_size = vdex_size
    page_aligned_size = round_up(vdex_size, PAGE_SIZE)

    # === Phase 1: WriteDexFiles ===
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, page_aligned_size)

        mm = mmap.mmap(fd, page_aligned_size, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE)
    except Exception:
        os.close(fd)
        raise

    try:
        for i in range(D):
            off = dex_offsets[i]
            mm[off:off + DEX_SIZE] = dex_bodies[i]

        # === Phase 3: FinishVdexFile ===
        verifier_deps = make_filler(0xDE, VERIFIER_DEPS_SIZE)
        lookup_tables = make_filler(0xBB, LOOKUP_TABLE_SIZE)
        tail_buffer = verifier_deps + lookup_tables

        verifier_deps_offset = round_up(vdex_size, 4)
        vdex_size = verifier_deps_offset + VERIFIER_DEPS_SIZE
        lookup_tables_offset = vdex_size
        vdex_size += LOOKUP_TABLE_SIZE

        os.ftruncate(fd, vdex_size)

        mmapped_end = page_aligned_size
        first_chunk_size = min(len(tail_buffer), mmapped_end - old_vdex_size)

        if first_chunk_size > 0:
            tail_start = old_vdex_size
            aligned_tail_start = round_up(old_vdex_size, 4)
            pad = aligned_tail_start - old_vdex_size
            mm[aligned_tail_start:aligned_tail_start + first_chunk_size - pad] = \
                tail_buffer[:first_chunk_size - pad]

        extra_mm = None
        if first_chunk_size < len(tail_buffer):
            extra_mm = mmap.mmap(fd, vdex_size - mmapped_end, flags=mmap.MAP_SHARED,
                                 prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                 offset=mmapped_end)
            remaining = tail_buffer[first_chunk_size:]
            extra_mm[0:len(remaining)] = remaining

        checksums_offset = SIZEOF_HEADER + NUM_SECTIONS * SIZEOF_SECTION_HDR
        for i in range(D):
            cksum = (file_idx * 31 + i) & 0xFFFFFFFF
            struct.pack_into("<I", mm, checksums_offset + i * 4, cksum)

        ptr = SIZEOF_HEADER
        sections = [
            (0, checksums_offset, D * SIZEOF_CHECKSUM),
            (1, dex_offsets[0], verifier_deps_offset - dex_offsets[0]),
            (2, verifier_deps_offset, VERIFIER_DEPS_SIZE),
            (3, lookup_tables_offset, LOOKUP_TABLE_SIZE),
        ]
        for kind, s_off, s_size in sections:
            struct.pack_into("<III", mm, ptr, kind, s_off, s_size)
            ptr += SIZEOF_SECTION_HDR

        # --- First msync: flush body (header magic still invalid) ---
        mm.flush(0, page_aligned_size)

        if extra_mm is not None:
            extra_mm.flush()
            extra_mm.close()

        # --- Write valid header LAST ---
        mm[0:4] = VDEX_MAGIC
        mm[4:8] = VDEX_VERSION
        struct.pack_into("<I", mm, 8, NUM_SECTIONS)

        # --- Second msync: flush first page only (header) ---
        mm.flush(0, PAGE_SIZE)

    finally:
        mm.close()
        os.fsync(fd)
        os.close(fd)


def stress_loop(loop_idx):
    paths = [os.path.join(WORK_DIR, f"vdex_{i:03d}.vdex") for i in range(CONCURRENCY)]

    for p in paths:
        if os.path.exists(p):
            os.unlink(p)

    threads = []
    t0 = time.monotonic()
    for i, p in enumerate(paths):
        t = threading.Thread(target=write_one_vdex, args=(p, loop_idx * CONCURRENCY + i))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    elapsed = time.monotonic() - t0
    total_bytes = CONCURRENCY * (DEX_SIZE * NUM_DEX_PER_VDEX + VERIFIER_DEPS_SIZE + LOOKUP_TABLE_SIZE)
    print(f"[loop {loop_idx}] {CONCURRENCY} vdex files, "
          f"{total_bytes / 1024 / 1024:.1f} MB, {elapsed:.3f}s, "
          f"{total_bytes / elapsed / 1024 / 1024:.1f} MB/s", flush=True)


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    print(f"[config] dir={WORK_DIR} concurrency={CONCURRENCY} "
          f"dex_per_vdex={NUM_DEX_PER_VDEX} dex_size={DEX_SIZE // 1024}KB "
          f"loops={'infinite' if LOOPS == 0 else LOOPS}", flush=True)

    loop_idx = 0
    try:
        while True:
            stress_loop(loop_idx)
            loop_idx += 1
            if LOOPS and loop_idx >= LOOPS:
                break
    except KeyboardInterrupt:
        print(f"\n[done] {loop_idx} loops completed", flush=True)


if __name__ == "__main__":
    main()
