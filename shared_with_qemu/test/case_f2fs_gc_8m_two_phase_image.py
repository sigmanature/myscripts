#!/usr/bin/env python3
import os
import sys
import time

# make "tests/utils" importable when running from repo root
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from utils.io import ensure_dir, open_rw, ensure_file_size, pread_scan, pwrite_pattern, fsync, sleep_s
from utils.sysutil import drop_caches_simple, is_root
from utils.loop_mount import LoopMount
from utils.f2fs_gc import GcPulseThread

# =========================
# 只改这里：不加 CLI 参数
# =========================
IMAGE_PATH = "/tmp/f2fs_gc_case_8m.img"
IMAGE_SIZE_BYTES = 512 * 1024 * 1024
MOUNTPOINT = "/mnt/f2fs_gc_case_8m"

GROUPS = 0  # 0 = run forever until Ctrl-C
FILE_SIZE = 8 * 1024 * 1024
READ_FIRST = 2 * 1024 * 1024
FRONT_WRITE = 1 * 1024 * 1024
TAIL_WRITE = 1 * 1024 * 1024
SLEEP_AFTER_FRONT = 10.0
BETWEEN_GROUPS = 0.2

GC_INTERVAL = 0.3
CHURN_INTERVAL = 0.05

VERBOSE = True
DROP_CACHES_EACH_GROUP = True  # 最简单实现：每组 pread 前 drop caches

# =========================

def prepare_layout(workdir: str) -> dict:
    """封装准备阶段：目录 + target 文件 + inline marker。"""
    normal_dir = os.path.join(workdir, "normal")
    inline_dir = os.path.join(workdir, "inline")
    churn_dir_normal = os.path.join(workdir, "churn_normal")
    churn_dir_inline = os.path.join(workdir, "churn_inline")

    for d in (workdir, normal_dir, inline_dir, churn_dir_normal, churn_dir_inline):
        ensure_dir(d)

    target_path = os.path.join(normal_dir, "target_8m.bin")
    inline_path = os.path.join(inline_dir, "inline_marker.bin")

    # Create/size target
    fd = open_rw(target_path, create=True)
    try:
        ensure_file_size(fd, FILE_SIZE)
        fsync(fd)
    finally:
        os.close(fd)

    # Create inline marker
    fd = open_rw(inline_path, create=True)
    try:
        ensure_file_size(fd, 2 * 1024)
        fsync(fd)
    finally:
        os.close(fd)

    return {
        "workdir": workdir,
        "normal_dir": normal_dir,
        "inline_dir": inline_dir,
        "churn_dir_normal": churn_dir_normal,
        "churn_dir_inline": churn_dir_inline,
        "target_path": target_path,
        "inline_path": inline_path,
    }

def one_group(fd: int, seed_base: int) -> None:
    # 1) read first 2MB (pread scan)
    pread_scan(fd, 0, READ_FIRST, chunk=256 * 1024, passes=1)

    # 2) write front 1MB + fsync
    pwrite_pattern(fd, 0, FRONT_WRITE, seed=seed_base + 1, chunk=256 * 1024)
    fsync(fd)

    # 3) wait "较久"
    sleep_s(SLEEP_AFTER_FRONT)

    # 4) write last 1MB + fsync
    tail_off = FILE_SIZE - TAIL_WRITE
    pwrite_pattern(fd, tail_off, TAIL_WRITE, seed=seed_base + 2, chunk=256 * 1024)
    fsync(fd)

def main():
    if not is_root():
        raise SystemExit("need root (losetup/mount/umount + drop_caches)")

    lm = LoopMount(image_path=IMAGE_PATH, mountpoint=MOUNTPOINT)
    lm.setup(image_size_bytes=IMAGE_SIZE_BYTES, verbose=VERBOSE)

    workdir = os.path.join(MOUNTPOINT, "gc_case_8m")
    layout = prepare_layout(workdir)

    # GC pulse thread: direct f2fs_io backend only
    gc_thr = GcPulseThread(mountpoint=MOUNTPOINT, interval_s=GC_INTERVAL, verbose=VERBOSE)
    gc_thr.start()

    # # churn threads (inline vs normal)
    # churn_inline = ChurnThread(
    #     churn_dir=layout["churn_dir_inline"],
    #     inline_mode=True,
    #     file_size=2 * 1024,
    #     files_per_round=64,
    #     keep_fraction=0.25,
    #     interval_s=CHURN_INTERVAL,
    #     seed=7,
    #     verbose=VERBOSE,
    # )
    # churn_normal = ChurnThread(
    #     churn_dir=layout["churn_dir_normal"],
    #     inline_mode=False,
    #     file_size=1 * 1024 * 1024,
    #     files_per_round=16,
    #     keep_fraction=0.25,
    #     interval_s=CHURN_INTERVAL,
    #     seed=11,
    #     verbose=VERBOSE,
    # )
    # churn_inline.start()
    # churn_normal.start()

    print(f"[*] mounted: {MOUNTPOINT}", flush=True)
    print(f"[*] target: {layout['target_path']} ({FILE_SIZE} bytes)", flush=True)
    print(f"[*] gc backend: {gc_thr.backend_desc}", flush=True)

    group = 0
    try:
        while True:
            group += 1

            # touch inline marker each group (separate path)
            fd_inl = open_rw(layout["inline_path"], create=True)
            try:
                ensure_file_size(fd_inl, 2 * 1024)
                pwrite_pattern(fd_inl, 0, 2 * 1024, seed=1000 + group)
                fsync(fd_inl)
            finally:
                os.close(fd_inl)

            if DROP_CACHES_EACH_GROUP:
                # 最简单 drop cache：每组真正 read 前做一次
                drop_caches_simple(verbose=VERBOSE)

            fd_t = open_rw(layout["target_path"], create=False)
            try:
                t0 = time.time()
                one_group(fd_t, seed_base=group * 100)
                dt = time.time() - t0
            finally:
                os.close(fd_t)

            if VERBOSE:
                print(
                    f"[group {group}] {dt:.3f}s | gc_pulses={gc_thr.pulses} ok={gc_thr.success} | ",
                    flush=True,
                )

            if GROUPS > 0 and group >= GROUPS:
                break

            sleep_s(BETWEEN_GROUPS)

    except KeyboardInterrupt:
        print("\n[!] Ctrl-C: stopping...", flush=True)
    finally:
        # churn_inline.stop()
        # churn_normal.stop()
        gc_thr.stop()
        time.sleep(0.2)
        lm.cleanup(verbose=VERBOSE)

    print("[*] exit.", flush=True)

if __name__ == "__main__":
    main()
