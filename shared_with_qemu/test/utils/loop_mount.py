import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Sequence

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
    mount_opts: str = "mode=lfs"
    mkfs_features: Optional[Sequence[str]] = None

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
        mkfs = ["mkfs.f2fs", "-f"]
        for feat in self.mkfs_features or ():
            mkfs.extend(["-O", str(feat)])
        mkfs.append(self.loopdev)
        _run(mkfs, check=True)

        # Mount the loop image in LFS mode so rewrite workload creates OPU
        # victims instead of staying on the small-volume IPU fast path.
        _run(
            ["mount", "-t", "f2fs", "-o", self.mount_opts, self.loopdev, self.mountpoint],
            check=True,
        )

        if verbose:
            print(f"[loop] image={self.image_path}", flush=True)
            print(f"[loop] loopdev={self.loopdev}", flush=True)
            print(f"[loop] mount_opts={self.mount_opts}", flush=True)
            print(f"[loop] mounted at {self.mountpoint}", flush=True)

    def unmount(self, verbose: bool = False, retries: int = 10, delay_s: float = 0.5) -> bool:
        last_stderr = ""
        for _ in range(retries):
            if not _is_mounted(self.mountpoint):
                return True
            _run(["sync"], check=False)
            cp = _run(["umount", self.mountpoint], check=False, capture=True)
            if cp.returncode == 0:
                return True
            last_stderr = (cp.stderr or cp.stdout or "").strip()
            time.sleep(delay_s)

        if verbose and last_stderr:
            print(f"[loop] umount failed: {last_stderr}", flush=True)
        return not _is_mounted(self.mountpoint)

    def mount_existing(self, verbose: bool = False) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("need root for mount")
        if not self.loopdev:
            raise RuntimeError("loopdev missing; call setup() first")
        if _is_mounted(self.mountpoint):
            raise RuntimeError(f"mountpoint already mounted: {self.mountpoint}")
        _ensure_dir(self.mountpoint)
        _run(
            ["mount", "-t", "f2fs", "-o", self.mount_opts, self.loopdev, self.mountpoint],
            check=True,
        )
        if verbose:
            print(f"[loop] remounted {self.loopdev} at {self.mountpoint}", flush=True)

    def cleanup(self, verbose: bool = False, remove_image: bool = True) -> None:
        # best effort cleanup
        try:
            if not self.unmount(verbose=verbose, retries=5, delay_s=0.5):
                _run(["umount", "-l", self.mountpoint], check=False)
            if _is_mounted(self.mountpoint):
                _run(["umount", "-l", self.mountpoint], check=False)
        except Exception:
            pass

        try:
            if self.loopdev:
                _run(["losetup", "-d", self.loopdev], check=False)
        except Exception:
            pass

        if remove_image:
            try:
                if os.path.exists(self.image_path):
                    os.unlink(self.image_path)
            except Exception:
                pass
        elif verbose:
            print(f"[loop] preserved image={self.image_path}", flush=True)

        try:
            # keep mountpoint dir (in case you want logs), but you can uncomment to remove:
            # shutil.rmtree(self.mountpoint, ignore_errors=True)
            pass
        except Exception:
            pass

        if verbose:
            print("[loop] cleanup done", flush=True)
