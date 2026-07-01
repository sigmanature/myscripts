#!/usr/bin/env python3 -u
"""Step 3 of create_and_fill_file: pwrite 8MB with baseline pattern"""
import sys, os, time
sys.path.insert(0, "/root/shared_with_host/test")
from utils.io import open_rw, pwrite_pattern_config, fsync
from utils.gc_two_phase import make_baseline_config

cfg = make_baseline_config(chunk_size=256*1024)
fd = open_rw("/mnt/f2fs/test_step/test.bin", create=False)
print("writing 8MB...", flush=True)
t0 = time.time()
pwrite_pattern_config(fd, 0, 8*1024*1024, cfg)
dt = time.time() - t0
print(f"pwrite done in {dt:.2f}s", flush=True)
os.fsync(fd)
os.close(fd)
print("all done", flush=True)
