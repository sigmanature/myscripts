#!/usr/bin/env python3
import os, sys

td = sys.argv[1]

# test 1: O_CREAT only
fd = os.open(td + "/t1.bin", os.O_RDWR | os.O_CREAT, 0o644)
print(f"O_CREAT: fd={fd}")
os.close(fd)

# test 2: O_CREAT | O_TRUNC
fd = os.open(td + "/t2.bin", os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
print(f"+O_TRUNC: fd={fd}")
os.close(fd)

# test 3: O_RDWR | O_CREAT | O_TRUNC with ftruncate
fd = os.open(td + "/t3.bin", os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
print(f"+O_TRUNC: fd={fd}")
os.ftruncate(fd, 4096)
print(f"ftruncate OK")
os.close(fd)

# test 4: mmap it
import mmap
fd = os.open(td + "/t3.bin", os.O_RDWR)
mm = mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
print(f"mmap: len={len(mm)}")
mm[0:4096] = b"X" * 4096
print(f"write OK")
mm.flush()
mm.close()
os.close(fd)
print("ALL OK")
