import os
import subprocess

def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except Exception:
        return False

def run(cmd, check=True):
    return subprocess.run(cmd, check=check)

def drop_caches_simple(verbose: bool = False) -> bool:
    """最简单 drop cache: sync; echo 3 > /proc/sys/vm/drop_caches (needs root)."""
    if not is_root():
        if verbose:
            print("[drop_caches] skip (not root)", flush=True)
        return False
    try:
        run(["sync"], check=False)
        with open("/proc/sys/vm/drop_caches", "w", encoding="utf-8") as f:
            f.write("3")
        if verbose:
            print("[drop_caches] OK", flush=True)
        return True
    except Exception as e:
        if verbose:
            print(f"[drop_caches] FAIL: {e}", flush=True)
        return False
