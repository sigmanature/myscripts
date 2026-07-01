#!/usr/bin/env python3
import os, mmap, sys

testdir = sys.argv[1]
testfile = testdir + "/mmap_test.bin"

fd = os.open(testfile, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
print(f"fd={fd}", flush=True)

size = 4096 * 256 + 2048
os.ftruncate(fd, size)
print(f"ftruncate({size}) OK", flush=True)

mm = mmap.mmap(fd, size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
print(f"mmap OK, len={len(mm)}", flush=True)

data = b"X" * 4096
mm[0:4096] = data
print("page 0 written", flush=True)
mm[4096*255:4096*255+4096] = data
print("page 255 written", flush=True)
mm[4096*256:4096*256+2048] = b"Y" * 2048
print("tail written", flush=True)

mm.flush()
print("mm.flush OK", flush=True)
mm.close()
os.fsync(fd)
os.close(fd)
print("fsync+close OK", flush=True)

st = os.stat(testfile)
print(f"file size={st.st_size}", flush=True)
with open(testfile, "rb") as f:
    first = f.read(4096)
    if first == data:
        print("VERIFY OK", flush=True)
    else:
        print(f"VERIFY FAIL: first byte={first[0]}", flush=True)

os.unlink(testfile)
print("DONE", flush=True)
