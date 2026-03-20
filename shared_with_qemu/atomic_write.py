#!/usr/bin/env python3
import os, fcntl, sys, errno

# ---------- ioctl numbers ----------
F2FS_IOC_START_ATOMIC_REPLACE  = 0xF519   # _IO(0xF5, 25)
F2FS_IOC_COMMIT_ATOMIC_WRITE   = 0xF502   # _IO(0xF5,  2)
F2FS_IOC_ABORT_ATOMIC_WRITE    = 0xF505   # _IO(0xF5,  5)
# -----------------------------------

def atomic_replace(path: str, payload: bytes):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)  # **不要 O_TRUNC**
    try:
        fcntl.ioctl(fd, F2FS_IOC_START_ATOMIC_REPLACE)

        if os.write(fd, payload) != len(payload):
            raise IOError(errno.EIO, "short write")

        fcntl.ioctl(fd, F2FS_IOC_COMMIT_ATOMIC_WRITE)
        os.fdatasync(fd)

    except Exception:
        fcntl.ioctl(fd, F2FS_IOC_ABORT_ATOMIC_WRITE, 0)
        raise
    finally:
        os.close(fd)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/mnt/f2fs/atomic.txt"
    data   = b"hello-atomic\n" * 4096          # 53 KiB
    atomic_replace(target, data)
    print(f"atomic-replace wrote {len(data)} bytes to {target}")
