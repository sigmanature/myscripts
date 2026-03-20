#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stable wp-fault (write-protect fault) reproducer for MAP_SHARED mmap writes on f2fs,
intended to hit f2fs_vm_page_mkwrite() and write exactly ONE 4K subpage inside
a (hopefully) large folio created by readahead.

Guarantees (if file is on f2fs + uses ->page_mkwrite):
  1) wp-fault happens (read first, then write).
  2) write touches only one byte within one 4K page (a "subpage" in a large folio).
  3) flush+fsync verifies persistence.

Not guaranteed from userspace alone:
  - the page cache folio covering target offset is large.
"""

import argparse
import ctypes
import errno
import mmap
import os
import struct
import sys
import time

PAGE = 4096
TARGET_LARGE = 64 * 1024          # "希望"的大 folio 尺寸例子：64K = 16*4K
FILE_SZ = 8 * 1024 * 1024         # 8MB
RA_SPAN = 2 * 1024 * 1024         # 2MB 顺序扫描促进 readahead

# SYS_readahead x86_64=187; 其他架构可能不同。我们用 libc 的 syscall 调它。
SYS_readahead = 187

libc = ctypes.CDLL(None, use_errno=True)

def die(msg: str):
    e = ctypes.get_errno()
    if e:
        raise OSError(e, f"{msg}: {os.strerror(e)}")
    raise RuntimeError(msg)

def round_down(x: int, a: int) -> int:
    return x & ~(a - 1)

def try_drop_caches():
    # 需要 root。失败就跳过。
    try:
        os.sync()
        with open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as f:
            f.write("3\n")
        return True
    except Exception:
        return False

def try_sys_readahead(fd: int, off: int, count: int) -> bool:
    # long syscall(long number, ...);
    ret = libc.syscall(ctypes.c_long(SYS_readahead),
                       ctypes.c_int(fd),
                       ctypes.c_long(off),
                       ctypes.c_size_t(count))
    if ret != 0:
        e = ctypes.get_errno()
        # 非致命：有些环境可能不允许或 syscall 号不匹配
        sys.stderr.write(f"[!] readahead syscall failed: {e} {os.strerror(e)} (non-fatal)\n")
        return False
    return True

def fill_file(fd: int, size: int, span: int):
    # 让文件有实际数据（尽量避免洞/hole 带来的路径差异）
    try:
        os.posix_fallocate(fd, 0, size)
    except AttributeError:
        # 旧 Python 可能没有 posix_fallocate
        os.ftruncate(fd, size)
    except OSError as e:
        sys.stderr.write(f"[!] posix_fallocate failed: {e} (continuing with ftruncate)\n")
        os.ftruncate(fd, size)

    # 写入 2MB 已知模式（用 64K 块写，符合“希望的大 folio”）
    chunk = bytearray(TARGET_LARGE)
    for i in range(len(chunk)):
        chunk[i] = (i ^ 0x5A) & 0xFF

    off = 0
    while off < span:
        n = os.pwrite(fd, chunk, off)
        if n != len(chunk):
            raise OSError(errno.EIO, f"pwrite short: {n}")
        off += len(chunk)

    os.fsync(fd)

def sequential_pread_scan(fd: int, span: int, buf_sz: int, passes: int = 2):
    buf = bytearray(buf_sz)
    for p in range(passes):
        off = 0
        while off < span:
            data = os.pread(fd, min(buf_sz, span - off), off)
            if not data:
                break
            # 把数据“用一下”，避免被解释器优化掉（虽然 os.pread 已经发生了）
            buf[0] ^= data[0]
            off += len(data)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="文件路径（必须在 f2fs 挂载点上）")
    ap.add_argument("--drop-caches", action="store_true", help="尝试 drop_caches（需要 root）")
    ap.add_argument("--do-readahead", action="store_true", help="尝试调用 readahead syscall（非必须）")
    ap.add_argument("--pdb", action="store_true", help="在写入前进入 pdb 断点")
    args = ap.parse_args()

    fd = os.open(args.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)

    try:
        fill_file(fd, FILE_SZ, RA_SPAN)

        if args.drop_caches:
            ok = try_drop_caches()
            print(f"[*] drop_caches: {'OK' if ok else 'SKIP/FAIL (need root?)'}")

        # 促进 readahead：先 syscall，再两遍顺序 pread 扫描
        if args.do_readahead:
            print("[*] calling readahead syscall...")
            try_sys_readahead(fd, 0, RA_SPAN)

        print("[*] sequential pread scan (2 passes) to ramp readahead...")
        sequential_pread_scan(fd, RA_SPAN, buf_sz=256 * 1024, passes=2)

        # mmap shared
        mm = mmap.mmap(fd, FILE_SZ, flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE)

        # 选择 64K 对齐窗口中的第 8 个 4K page（subpage）
        base = round_down(512 * 1024, TARGET_LARGE)
        target = base + 7 * PAGE
        if target + PAGE > FILE_SZ:
            raise RuntimeError("target out of range")

        print(f"[*] target offset = {target} (base {base}, subpage {7}/16 of 64K window)")

        # 关键：先读后写，稳定触发 wp-fault（PTE present but write-protected -> do_shared_fault->page_mkwrite）
        old = mm[target]
        _ = old  # keep

        if args.pdb:
            import pdb
            pdb.set_trace()

        newv = (old ^ 0x01) & 0xFF
        # 写 1 byte（只触碰一个 subpage 内的一个字节）
        mm[target] = newv

        # flush 对应 msync(MS_SYNC)（Python 的 flush 是 best-effort 等价）
        mm.flush(target, PAGE)
        os.fsync(fd)

        disk = os.pread(fd, 1, target)
        if len(disk) != 1 or disk[0] != newv:
            print(f"FAIL: disk mismatch at {target}: got {disk[0] if disk else None} expected {newv}")
            return 1

        print(f"PASS: wrote 1 byte at {target}: {old:#04x} -> {newv:#04x}, flush+fsync persisted.")

        mm.close()
        return 0

    finally:
        os.close(fd)

if __name__ == "__main__":
    sys.exit(main())
