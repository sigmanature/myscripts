#!/usr/bin/env python3
import os
import sys
import time

# make "tests/utils" importable when running from repo root
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from utils.io import ensure_dir, open_rw, ensure_file_size, fsync, pwrite_pattern_config
from utils.sysutil import drop_caches, is_root
from utils.loop_mount import LoopMount
from utils.f2fs_gc import GcPulseThread
from utils.gc_two_phase import TwoPhaseSpec, init_file_with_baseline, make_baseline_config, run_two_phase_group_and_verify
from utils.fscrypt_inline import ensure_inline_fscrypt_dir
from utils.patterns import PatternConfig

# =========================
# 只改这里：不加 CLI 参数
# =========================
IMAGE_PATH = "/tmp/f2fs_gc_case_8m_inline.img"
IMAGE_SIZE_BYTES = 512 * 1024 * 1024
MOUNTPOINT = "/mnt/f2fs_gc_case_8m_inline"

ENC_DIR_NAME = "enc_test"
FSCRYPT_KEY = "/opt/test-secrets/fscrypt-ci.key"
PROTECTOR_NAME = "gc-case-inline"

GROUPS = 0  # 0 = run forever until Ctrl-C
FILE_SIZE = 8 * 1024 * 1024
READ_FIRST = 2 * 1024 * 1024
FRONT_WRITE = 1 * 1024 * 1024
TAIL_WRITE = 1 * 1024 * 1024
SLEEP_AFTER_FRONT = 10.0
BETWEEN_GROUPS = 0.2

GC_INTERVAL = 0.3

VERBOSE = True
DROP_CACHES_EACH_GROUP = True
VERIFY_DISK_EACH_GROUP = True

# =========================

SPEC = TwoPhaseSpec(
    file_size=FILE_SIZE,
    read_first=READ_FIRST,
    front_write=FRONT_WRITE,
    tail_write=TAIL_WRITE,
    sleep_after_front=SLEEP_AFTER_FRONT,
)


def prepare_target(enc_root: str) -> str:
    workdir = os.path.join(enc_root, "gc_case_8m_inline")
    ensure_dir(workdir)
    target_path = os.path.join(workdir, "target_8m.bin")
    fd = open_rw(target_path, create=True)
    try:
        ensure_file_size(fd, FILE_SIZE)
        fsync(fd)
    finally:
        os.close(fd)
    return target_path


def main() -> None:
    if not is_root():
        raise SystemExit("need root (losetup/mount/umount + drop_caches)")

    lm = LoopMount(
        image_path=IMAGE_PATH,
        mountpoint=MOUNTPOINT,
        mount_opts="mode=lfs,inlinecrypt",
        mkfs_features=("encrypt",),
    )
    lm.setup(image_size_bytes=IMAGE_SIZE_BYTES, verbose=VERBOSE)

    enc_root = ensure_inline_fscrypt_dir(
        mount_root=MOUNTPOINT,
        enc_dir_name=ENC_DIR_NAME,
        key_path=FSCRYPT_KEY,
        protector_name=PROTECTOR_NAME,
    )

    target_path = prepare_target(enc_root)

    baseline = make_baseline_config(chunk_size=256 * 1024)
    init_file_with_baseline(target_path, FILE_SIZE, baseline=baseline)

    gc_thr = GcPulseThread(mountpoint=MOUNTPOINT, interval_s=GC_INTERVAL, verbose=VERBOSE)
    gc_thr.start()

    print(f"[*] mounted: {MOUNTPOINT}", flush=True)
    print(f"[*] enc_root: {enc_root}", flush=True)
    print(f"[*] target: {target_path} ({FILE_SIZE} bytes)", flush=True)
    print(f"[*] gc backend: {gc_thr.backend_desc}", flush=True)

    group = 0
    try:
        while True:
            group += 1

            drop_caches(3)

            t0 = time.time()
            ok = run_two_phase_group_and_verify(
                target_path,
                spec=SPEC,
                seed_base=group * 100,
                baseline=baseline,
                chunk_size=256 * 1024,
                verify_disk=VERIFY_DISK_EACH_GROUP,
            )
            dt = time.time() - t0
            if not ok:
                raise SystemExit(f"disk verify failed at group={group}")

            if VERBOSE:
                print(
                    f"[group {group}] {dt:.3f}s | gc_pulses={gc_thr.pulses} ok={gc_thr.success} | ",
                    flush=True,
                )

            if GROUPS > 0 and group >= GROUPS:
                break
            time.sleep(BETWEEN_GROUPS)

    except KeyboardInterrupt:
        print("\n[!] Ctrl-C: stopping...", flush=True)
    finally:
        gc_thr.stop()
        time.sleep(0.2)
        lm.cleanup(verbose=VERBOSE)

    print("[*] exit.", flush=True)


if __name__ == "__main__":
    main()

