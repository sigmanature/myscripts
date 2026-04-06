import os
import shutil
import subprocess
import threading
import time
from typing import Optional

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

def _resolve_f2fs_io() -> Optional[str]:
    return shutil.which("f2fs_io")


def _device_name_from_mount(mountpoint: str) -> Optional[str]:
    src = _mount_source(mountpoint)
    if not src:
        return None
    return os.path.basename(os.path.realpath(src))


def trigger_urgent_gc(f2fs_io_path: str, dev_name: str, gc_window_s: int) -> bool:
    cp = subprocess.run(
        [f2fs_io_path, "gc_urgent", dev_name, "run", str(gc_window_s)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cp.returncode == 0

class GcPulseThread(threading.Thread):
    def __init__(
        self,
        mountpoint: str,
        interval_s: float = 0.5,
        gc_window_s: int = 1,
        verbose: bool = False,
    ):
        super().__init__(daemon=True)
        self.mountpoint = mountpoint
        self.interval_s = interval_s
        self.gc_window_s = max(1, int(gc_window_s))
        self.verbose = verbose
        self._stop = threading.Event()
        self.pulses = 0
        self.success = 0
        self.f2fs_io_path = _resolve_f2fs_io()
        self.dev_name = _device_name_from_mount(mountpoint)
        self.backend_desc = self._build_backend_desc()

    def _build_backend_desc(self) -> str:
        if not self.f2fs_io_path:
            return "f2fs_io missing from PATH"
        if not self.dev_name:
            return f"f2fs_io ready but no f2fs device for {self.mountpoint}"
        return (
            f"f2fs_io gc_urgent dev={self.dev_name} "
            f"window={self.gc_window_s}s interval={self.interval_s}s"
        )

    def ready(self) -> bool:
        return bool(self.f2fs_io_path and self.dev_name)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.ready():
            if self.verbose:
                print(f"[gc] {self.backend_desc}; GC pulse thread idle", flush=True)
            while not self._stop.is_set():
                time.sleep(1.0)
            return

        if self.verbose:
            print(f"[gc] backend={self.backend_desc}", flush=True)

        while not self._stop.is_set():
            self.pulses += 1
            if trigger_urgent_gc(self.f2fs_io_path, self.dev_name, self.gc_window_s):
                self.success += 1
            time.sleep(self.interval_s)
