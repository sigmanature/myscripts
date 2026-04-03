import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

def _run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)

def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _is_mounted(mountpoint: str) -> bool:
    mp = os.path.realpath(mountpoint)
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and os.path.realpath(parts[1]) == mp:
                    return True
    except Exception:
        return False
    return False

@dataclass
class LoopMount:
    image_path: str
    mountpoint: str
    loopdev: Optional[str] = None

    def setup(self, image_size_bytes: int, verbose: bool = False) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("need root for losetup/mount/umount")

        if _is_mounted(self.mountpoint):
            raise RuntimeError(f"mountpoint already mounted: {self.mountpoint}")

        _ensure_dir(os.path.dirname(self.image_path) or ".")
        _ensure_dir(self.mountpoint)

        # recreate image from scratch
        if os.path.exists(self.image_path):
            os.unlink(self.image_path)
        with open(self.image_path, "wb") as f:
            f.truncate(int(image_size_bytes))

        # losetup
        cp = _run(["losetup", "--find", "--show", self.image_path], check=True, capture=True)
        self.loopdev = cp.stdout.strip()

        # mkfs.f2fs (force)
        _run(["mkfs.f2fs", "-f", self.loopdev], check=True)

        # mount
        _run(["mount", "-t", "f2fs", self.loopdev, self.mountpoint], check=True)

        if verbose:
            print(f"[loop] image={self.image_path}", flush=True)
            print(f"[loop] loopdev={self.loopdev}", flush=True)
            print(f"[loop] mounted at {self.mountpoint}", flush=True)

    def cleanup(self, verbose: bool = False) -> None:
        # best effort cleanup
        try:
            if _is_mounted(self.mountpoint):
                _run(["umount", self.mountpoint], check=False)
        except Exception:
            pass

        try:
            if self.loopdev:
                _run(["losetup", "-d", self.loopdev], check=False)
        except Exception:
            pass

        try:
            if os.path.exists(self.image_path):
                os.unlink(self.image_path)
        except Exception:
            pass

        try:
            # keep mountpoint dir (in case you want logs), but you can uncomment to remove:
            # shutil.rmtree(self.mountpoint, ignore_errors=True)
            pass
        except Exception:
            pass

        if verbose:
            print("[loop] cleanup done", flush=True)
