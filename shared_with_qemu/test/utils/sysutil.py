import os
import subprocess

def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except Exception:
        return False

def run(cmd, check=True):
    return subprocess.run(cmd, check=check)


def drop_caches(level: int) -> None:
    if level not in (1, 2, 3):
        raise ValueError("drop_caches level must be 1/2/3")
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as f:
        f.write(str(level))
    print("[drop_caches] OK", flush=True)
