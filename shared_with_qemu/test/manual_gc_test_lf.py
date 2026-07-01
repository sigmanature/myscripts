#!/usr/bin/env python3 -u
"""Manual GC + large folio test. Run from guest: python3 /root/shared_with_host/test/manual_gc_test_lf.py"""
import os, sys, time

sys.path.insert(0, "/root/shared_with_host/test")
from utils.io import create_and_fill_file, ensure_dir, open_rw, pwrite_pattern_config, fsync, sleep_s, pread_scan, drop_caches
from utils.f2fs_gc import GcPulseThread
from utils.gc_two_phase import make_baseline_config, make_mod251_config
from utils.patterns import PatternConfig

MOUNTPOINT = "/mnt/f2fs"
WORKDIR = os.path.join(MOUNTPOINT, "gc_case_lf")
TARGET = os.path.join(WORKDIR, "target_8m.bin")
FILE_SIZE = 8 * 1024 * 1024

ensure_dir(WORKDIR)

print("[1] create_and_fill_file (foliows 3-step protocol for large folio)...", flush=True)
baseline = make_baseline_config()
create_and_fill_file(TARGET, FILE_SIZE, baseline)
print("[1] done", flush=True)

print("[2] start GC pulse thread...", flush=True)
gc_thr = GcPulseThread(mountpoint=MOUNTPOINT, interval_s=0.3, verbose=True)
gc_thr.start()
time.sleep(1)

print("[3] two-phase loop: pread front -> pwrite front -> sleep 10s -> pwrite tail -> fsync", flush=True)
SPEC = (FILE_SIZE, 2*1024*1024, 1*1024*1024, 1*1024*1024)

for group in range(1, 121):
    drop_caches(3)
    fd = open_rw(TARGET, create=False)
    try:
        pread_scan(fd, 0, SPEC[1], chunk=256*1024, passes=1)
        front_cfg = make_mod251_config(seed=group*100+1)
        pwrite_pattern_config(fd, 0, SPEC[2], front_cfg)
        fsync(fd)
        sleep_s(10.0)
        tail_off = SPEC[0] - SPEC[3]
        tail_cfg = make_mod251_config(seed=group*100+2)
        pwrite_pattern_config(fd, tail_off, SPEC[3], tail_cfg)
        fsync(fd)
    finally:
        os.close(fd)
    print(f"[group {group}] gc_pulses={gc_thr.pulses}", flush=True)
    sys.stdout.flush()

gc_thr.stop()
print("[*] Done", flush=True)
