import os
import time
import threading
from typing import Optional

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return None

def _write_text(path: str, s: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        return True
    except Exception:
        return False

def _mount_source(mountpoint: str) -> Optional[str]:
    mp = os.path.realpath(mountpoint)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src, mnt, fstype = parts[0], parts[1], parts[2]
                if os.path.realpath(mnt) == mp and fstype == "f2fs":
                    return src
    except Exception:
        return None
    return None

def find_f2fs_sysfs_dir(mountpoint: str) -> Optional[str]:
    sysfs_root = "/sys/fs/f2fs"
    if not os.path.isdir(sysfs_root):
        return None

    src = _mount_source(mountpoint)
    src_base = os.path.basename(src) if src else None

    cands = []
    for name in os.listdir(sysfs_root):
        d = os.path.join(sysfs_root, name)
        if not os.path.isdir(d):
            continue
        devname = _read_text(os.path.join(d, "devname"))
        if devname:
            cands.append((d, devname))
        else:
            dev = _read_text(os.path.join(d, "dev"))
            if dev:
                cands.append((d, dev))

    if not cands:
        return None

    if src_base:
        for d, meta in cands:
            if meta == src_base or meta.endswith("/" + src_base) or meta.endswith(src_base):
                return d

    return cands[0][0]

def trigger_urgent_gc(sysfs_dir: str) -> bool:
    if not sysfs_dir:
        return False
    ok = False
    for knob in ("gc_urgent_high", "gc_urgent"):
        p = os.path.join(sysfs_dir, knob)
        if os.path.exists(p):
            ok = _write_text(p, "1") or ok
    return ok

class GcPulseThread(threading.Thread):
    def __init__(self, sysfs_dir: Optional[str], interval_s: float = 0.5, verbose: bool = False):
        super().__init__(daemon=True)
        self.sysfs_dir = sysfs_dir
        self.interval_s = interval_s
        self.verbose = verbose
        self._stop = threading.Event()
        self.pulses = 0
        self.success = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.sysfs_dir:
            if self.verbose:
                print("[gc] sysfs_dir not found; GC pulse thread idle", flush=True)
            while not self._stop.is_set():
                time.sleep(1.0)
            return

        if self.verbose:
            print(f"[gc] sysfs_dir={self.sysfs_dir}", flush=True)

        while not self._stop.is_set():
            self.pulses += 1
            if trigger_urgent_gc(self.sysfs_dir):
                self.success += 1
            time.sleep(self.interval_s)
