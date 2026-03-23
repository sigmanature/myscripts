#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rw_test_refactored.py

Refactor goals (based on rw_test.py):
- Reduce duplication.
- Support non-zero + non-block-aligned offset and size.
- Support:
  * write (w) with optional verify
  * verify-write (v) that can also operate on existing files (--allow-existing)
  * verify-only (no write)
- Pattern generation supports:
  * stream (constant memory)
  * direct (allocate full pattern in memory to amplify memory pressure)
- 'disk' verify path for buffered I/O prototyping:
  * fsync
  * sync
  * drop_caches
  * reopen + readback verify

NOTE: drop_caches requires root and impacts global system cache.
"""

import argparse
import os
import sys
import time
from typing import Iterator, Tuple, Optional

# -----------------------------
# Utility
# -----------------------------

def parse_bytes(size_str: str) -> int:
    """Parse '123', '4k', '10m', '1g', '512b' into bytes."""
    s = str(size_str).strip().lower()
    if not s:
        raise ValueError("Size string cannot be empty")

    unit = s[-1]
    if unit.isalpha():
        num_part = s[:-1]
        if unit == 'k':
            factor = 1024
        elif unit == 'm':
            factor = 1024 * 1024
        elif unit == 'g':
            factor = 1024 * 1024 * 1024
        elif unit == 'b':
            factor = 1
        else:
            raise ValueError(f"Invalid unit '{unit}' in '{s}'")
    else:
        num_part = s
        factor = 1

    if not num_part.isdigit():
        if len(s) == 1 and s.isalpha():
            raise ValueError(f"Missing number before unit in '{s}'")
        raise ValueError(f"Invalid number part '{num_part}' in '{s}'")

    return int(num_part) * factor


def hex_dump(data: bytes, bytes_per_line: int = 16) -> str:
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i + bytes_per_line]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        hex_part = hex_part.ljust(bytes_per_line * 3 - 1)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{i:08x}  {hex_part}  |{ascii_part}|")
    return '\n'.join(lines)


def dump_aligned_blocks(filename: str,
                        offset_bytes: int,
                        size_bytes: int,
                        block_size: int = 4096,
                        max_dump_bytes: int = 64 * 1024,
                        title: str = "") -> None:
    """Dump block-aligned region intersecting [offset, offset+size). Debug helper."""
    if size_bytes <= 0:
        print(f"[dump] size=0, skip. {title}".strip())
        return

    start = (offset_bytes // block_size) * block_size
    end = ((offset_bytes + size_bytes + block_size - 1) // block_size) * block_size
    total = end - start

    if total <= 0:
        print(f"[dump] nothing to dump. {title}".strip())
        return

    print("\n" + "=" * 70)
    if title:
        print(f"[Block-aligned dump] {title}")
    print(f"block_size={block_size}  aligned_range=[{start}, {end})  total={total} bytes")
    print(f"target_range=[{offset_bytes}, {offset_bytes + size_bytes})")

    if total > max_dump_bytes:
        print(f"(Output truncated) total {total} > max_dump_bytes {max_dump_bytes}, only dumping first {max_dump_bytes} bytes.")
        end = start + max_dump_bytes
        total = max_dump_bytes

    try:
        with open(filename, "rb") as f:
            f.seek(start)
            data = f.read(total)
    except Exception as e:
        print(f"[dump] Error reading file for dump: {e}")
        print("=" * 70)
        return

    print(f"(Base offset shown below is relative; add {start} to get file offsets)")
    print(hex_dump(data))
    print("=" * 70 + "\n")


# -----------------------------
# Pattern generation
# -----------------------------

def _repeat_bytes(token: bytes, start_idx: int, n: int) -> bytes:
    """Return n bytes repeating token, starting from token[start_idx]."""
    if n <= 0:
        return b""
    if not token:
        raise ValueError("pattern token cannot be empty")

    L = len(token)
    start_idx %= L

    # Build with minimal overhead.
    out = bytearray(n)
    # First partial
    first = min(n, L - start_idx)
    out[:first] = token[start_idx:start_idx + first]
    filled = first
    while filled < n:
        take = min(L, n - filled)
        out[filled:filled + take] = token[:take]
        filled += take
    return bytes(out)


def iter_expected_chunks(
    offset: int,
    size: int,
    mode: str,
    token: bytes,
    seed: int,
    chunk_size: int,
) -> Iterator[Tuple[int, bytes]]:
    """Yield (relative_pos, expected_bytes_chunk) for [offset, offset+size)."""
    pos = 0
    while pos < size:
        n = min(chunk_size, size - pos)

        if mode == 'counter':
            base = seed + offset + pos
            # byte = (base + i) & 0xFF
            b = bytes(((base + i) & 0xFF) for i in range(n))
        elif mode == 'repeat':
            # region-local repeat: starts at token[0] for this region
            start_idx = pos
            b = _repeat_bytes(token, start_idx, n)
        elif mode == 'filepos':
            # file-position anchored repeat: starts based on absolute file pos
            start_idx = offset + pos
            b = _repeat_bytes(token, start_idx, n)
        else:
            raise ValueError(f"Unknown pattern mode: {mode}")

        yield pos, b
        pos += n

def generate_expected_direct(
    offset: int,
    size: int,
    mode: str,
    token: bytes,
    seed: int,
    chunk_size: int,
) -> bytes:
    """Generate full expected bytes into memory (stress test friendly)."""
    # Use chunk join so filepos/repeat/counter all share logic.
    parts = []
    for _, b in iter_expected_chunks(offset, size, mode, token, seed, chunk_size):
        parts.append(b)
    return b"".join(parts)


# -----------------------------
# Buffered I/O helpers
# -----------------------------

def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)

def ensure_size_sparse(path: str, required_size: int) -> None:
    """Ensure file size >= required_size by ftruncate (sparse extension)."""
    if required_size <= 0:
        return
    st = os.stat(path)
    if st.st_size >= required_size:
        return
    with open(path, 'r+b') as f:
        os.ftruncate(f.fileno(), required_size)


def drop_caches(level: int) -> None:
    """Drop global caches. Requires root. level: 1,2,3."""
    if level not in (1, 2, 3):
        raise ValueError("drop_caches level must be 1/2/3")
    # Best practice: sync before dropping.
    os.sync()
    with open('/proc/sys/vm/drop_caches', 'w') as f:
        f.write(str(level))


def open_fd_for_write(path: str) -> int:
    flags = os.O_RDWR | os.O_CREAT
    # default perms: rw-r--r--
    return os.open(path, flags, 0o644)


def open_fd_for_read(path: str) -> int:
    return os.open(path, os.O_RDONLY)


def pwrite_all(fd: int, file_offset: int, data_iter: Iterator[Tuple[int, bytes]]) -> int:
    total = 0
    for rel_pos, chunk in data_iter:
        if not chunk:
            continue
        # os.pwrite returns bytes written
        n = os.pwrite(fd, chunk, file_offset + rel_pos)
        total += n
        if n != len(chunk):
            raise OSError(f"Short pwrite: wrote {n} expected {len(chunk)}")
    return total


def verify_stream(
    fd: int,
    file_offset: int,
    size: int,
    expected_direct: Optional[bytes],
    expected_iter: Optional[Iterator[Tuple[int, bytes]]],
    chunk_size: int,
    read_mode: str,
    max_mismatch_show: int = 16,
) -> bool:
    """Verify [file_offset, file_offset+size) using either direct expected or iterator.

    read_mode:
      - 'stream': pread chunk by chunk
      - 'direct': pread full (allocates size bytes)
    """
    if size <= 0:
        print("[verify] size=0, nothing to compare")
        return True

    def show_mismatch(pos: int, exp: bytes, got: bytes) -> None:
        print(f"\n[FAIL] mismatch at +{pos} (file_off={file_offset + pos})")
        show_len = min(256, len(exp), len(got))
        print("Expected (first up to 256 bytes of this chunk):")
        print(hex_dump(exp[:show_len]))
        print("Actual (first up to 256 bytes of this chunk):")
        print(hex_dump(got[:show_len]))

    if read_mode == 'direct':
        got = os.pread(fd, size, file_offset)
        if len(got) != size:
            print(f"[FAIL] readback short: got {len(got)} expected {size}")
            return False
        if expected_direct is None:
            # Build expected now (still direct compare, but expected is produced by iter)
            expected_direct = b"".join(b for _, b in expected_iter)  # type: ignore
        if got == expected_direct:
            print("[OK] verify pass (direct compare)")
            return True
        # Find first mismatch
        limit = min(len(got), len(expected_direct))
        mismatches = 0
        for i in range(limit):
            if got[i] != expected_direct[i]:
                # Show around mismatch
                lo = max(0, i - 64)
                hi = min(limit, i + 64)
                show_mismatch(i, expected_direct[lo:hi], got[lo:hi])
                mismatches += 1
                if mismatches >= max_mismatch_show:
                    break
        return False

    # stream compare
    if expected_direct is not None:
        # slice from expected_direct
        pos = 0
        while pos < size:
            n = min(chunk_size, size - pos)
            got = os.pread(fd, n, file_offset + pos)
            exp = expected_direct[pos:pos + n]
            if got != exp:
                show_mismatch(pos, exp, got)
                return False
            pos += n
        print("[OK] verify pass (stream compare vs direct expected)")
        return True

    # expected as iterator
    assert expected_iter is not None
    for rel_pos, exp in expected_iter:
        got = os.pread(fd, len(exp), file_offset + rel_pos)
        if got != exp:
            show_mismatch(rel_pos, exp, got)
            return False
    print("[OK] verify pass (stream compare)")
    return True


# -----------------------------
# Operations
# -----------------------------

def op_write(args) -> bool:
    """Write, optional verify."""
    ensure_parent_dir(args.file)

    preexisted = os.path.exists(args.file)
    if not preexisted:
        # create empty first
        open(args.file, 'ab').close()
    ensure_size_sparse(args.file, args.offset + args.size)

    token = args.token.encode('utf-8')

    if args.dump:
        dump_aligned_blocks(args.file, args.offset, args.size, block_size=args.block_size,
                           max_dump_bytes=args.dump_max, title="BEFORE write")

    # expected source
    expected_direct = None
    expected_iter = None

    if args.pattern_gen == 'direct':
        print(f"[pattern] generating expected DIRECT into memory: {args.size} bytes")
        t0 = time.perf_counter()
        expected_direct = generate_expected_direct(args.offset, args.size, args.pattern_mode, token, args.seed, args.chunk)
        t1 = time.perf_counter()
        print(f"[pattern] direct generation done in {t1 - t0:.3f}s")
        expected_iter = iter([(0, expected_direct)])  # for write, single chunk
    else:
        expected_iter = iter_expected_chunks(args.offset, args.size, args.pattern_mode, token, args.seed, args.chunk)

    fd = None
    try:
        fd = open_fd_for_write(args.file)
        written = pwrite_all(fd, args.offset, expected_iter)
        print(f"[write] wrote {written} bytes")
        os.fsync(fd) if args.fsync else None
        if args.fsync:
            print("[write] fsync done")
    except Exception as e:
        print(f"[FAIL] write error: {e}")
        return False
    finally:
        if fd is not None:
            os.close(fd)

    if args.dump:
        dump_aligned_blocks(args.file, args.offset, args.size, block_size=args.block_size,
                           max_dump_bytes=args.dump_max, title="AFTER write")

    if not args.verify:
        return True

    # verify after write
    return do_verify(args, preexisted=preexisted)


def op_verify_write(args) -> bool:
    """Verify-write (v): write + verify, with safety options."""
    ensure_parent_dir(args.file)

    preexisted = os.path.exists(args.file)
    if not preexisted:
        open(args.file, 'ab').close()

    # For new file in verify-write, we often want to ensure there is some tail beyond range,
    # but we keep it optional. We just ensure file size covers the write.
    ensure_size_sparse(args.file, args.offset + args.size)

    # Use op_write but force verify=True
    args.verify = True
    return op_write(args)


def do_verify(args, preexisted: bool) -> bool:
    """Verify-only core (can be used after write)."""
    if not os.path.exists(args.file):
        print(f"[FAIL] file not exist: {args.file}")
        return False

    token = args.token.encode('utf-8')

    # Prepare expected
    expected_direct = None
    expected_iter = None
    if args.pattern_gen == 'direct':
        print(f"[pattern] generating expected DIRECT into memory: {args.size} bytes")
        t0 = time.perf_counter()
        expected_direct = generate_expected_direct(args.offset, args.size, args.pattern_mode, token, args.seed, args.chunk)
        t1 = time.perf_counter()
        print(f"[pattern] direct generation done in {t1 - t0:.3f}s")
    else:
        expected_iter = iter_expected_chunks(args.offset, args.size, args.pattern_mode, token, args.seed, args.chunk)

    # (Optional) dump BEFORE dropping caches (disk-mode) or BEFORE verify read (cache-mode)
    if args.dump and args.verify_mode != 'disk':
        dump_aligned_blocks(args.file, args.offset, args.size, block_size=args.block_size,
                           max_dump_bytes=args.dump_max, title=f"BEFORE verify read ({args.verify_mode})")

    if args.verify_mode == 'disk':
        # For prototype: sync + drop_caches, then reopen for read.
        # IMPORTANT: dumping/reading the file AFTER dropping caches will warm page cache again and
        # make the verify become memory-vs-memory. So we do NOT dump after drop here.
        drop_level = 3

        if args.dump:
            dump_aligned_blocks(args.file, args.offset, args.size, block_size=args.block_size,
                               max_dump_bytes=args.dump_max, title="BEFORE drop_caches (disk verify)")

        print(f"[verify] disk-mode: os.sync + drop_caches({drop_level}) + reopen")
        try:
            t0 = time.perf_counter()
            drop_caches(drop_level)
            t1 = time.perf_counter()
            print(f"[verify] drop_caches done in {t1 - t0:.3f}s")
        except PermissionError:
            print("[FAIL] drop_caches requires root (permission denied).")
            print("       Try: sudo -E ./rw_test_refactored.py ... --verify-mode disk")
            return False
        except Exception as e:
            print(f"[FAIL] drop_caches error: {e}")
            return False


    fd = None
    try:
        fd = open_fd_for_read(args.file)
        if expected_direct is not None:
            ok = verify_stream(fd, args.offset, args.size, expected_direct, None, args.chunk, args.readback)
        else:
            # Need a fresh iterator for verify since iterators are consumed.
            expected_iter2 = iter_expected_chunks(args.offset, args.size, args.pattern_mode, token, args.seed, args.chunk)
            ok = verify_stream(fd, args.offset, args.size, None, expected_iter2, args.chunk, args.readback)
        return ok
    except Exception as e:
        print(f"[FAIL] verify error: {e}")
        return False
    finally:
        if fd is not None:
            os.close(fd)

def op_read(args) -> bool:
    if not os.path.exists(args.file):
        print(f"[FAIL] file not exist: {args.file}")
        return False

    if args.size <= 0:
        print("[read] size=0")
        return True

    fd = None
    try:
        fd = open_fd_for_read(args.file)
        data = os.pread(fd, args.size, args.offset)
        print(f"[read] got {len(data)} bytes")
        if args.dump:
            print(hex_dump(data[:min(len(data), args.dump_max)]))
        return True
    except Exception as e:
        print(f"[FAIL] read error: {e}")
        return False
    finally:
        if fd is not None:
            os.close(fd)


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Buffered I/O rw/verify test tool (refactored).",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    p.add_argument('operation',
                   choices=['read', 'write', 'verify', 'r', 'w', 'v'],
                   help="operation: read(r) / write(w) / verify-write(v) / verify-only")
    p.add_argument('offset', type=str, help='offset (supports b/k/m/g)')
    p.add_argument('size', type=str, help='size (supports b/k/m/g)')

    p.add_argument('-f', '--file', default='/tmp/rw_test.bin', help='target file')
    p.add_argument('-k', '--keep-file', action='store_true', help='do not delete file created by this run')


    # Sync
    p.add_argument('--fsync', action='store_true', default=True, help='call fsync after write [default on]')
    p.add_argument('--no-fsync', action='store_false', dest='fsync', help='disable fsync')

    # Verify options
    p.add_argument('--verify', action='store_true', help='(write) do verify after write')
    p.add_argument('--verify-mode', choices=['cache', 'disk'], default='cache',
                   help="verify readback mode: cache=normal read (may hit pagecache); disk=drop_caches then read")

    # Pattern options
    p.add_argument('--pattern-mode', choices=['repeat', 'filepos', 'counter'], default='repeat',
                   help='pattern mode: repeat(region-local), filepos(file-offset anchored), counter(byte=seed+pos)')
    p.add_argument('--token', default='PyWrtDta',
                   help='pattern token used by repeat/filepos modes (utf-8). default=PyWrtDta')
    p.add_argument('--seed', type=int, default=0, help='seed for counter mode')

    # Memory behavior
    p.add_argument("-pg",'--pattern-gen', choices=['stream', 'direct'], default='stream',
                   help='expected pattern generation: stream(const mem) or direct(alloc full bytes)')
    p.add_argument('--readback', choices=['stream', 'direct'], default='stream',
                   help='verify readback: stream(pread chunk) or direct(pread full region into memory)')
    p.add_argument('--chunk', type=str, default='1m',
                   help='chunk size for streaming (supports b/k/m/g). default=1m')

    # Debug dump
    p.add_argument("-dp",'--dump', action='store_true', help='dump aligned blocks / read hex (debug)')
    p.add_argument("-bs",'--block-size', type=str, default='4k', help='dump block size (default 4k)')
    p.add_argument('--dump-max', type=str, default='256k', help='max dump bytes (default 256k)')

    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()

    try:
        args.offset = parse_bytes(args.offset)
        args.size = parse_bytes(args.size)
        args.chunk = parse_bytes(args.chunk)
        args.block_size = parse_bytes(args.block_size)
        args.dump_max = parse_bytes(args.dump_max)
    except ValueError as e:
        print(f"Error parsing args: {e}", file=sys.stderr)
        p.print_usage()
        return 2

    if args.offset < 0 or args.size < 0:
        print("[FAIL] offset/size cannot be negative")
        return 2

    preexisted = os.path.exists(args.file)

    op = args.operation
    if op == 'r':
        op = 'read'
    elif op == 'w':
        op = 'write'
    elif op == 'v':
        op = 'verify'

    ok = False
    try:
        if op == 'read':
            ok = op_read(args)
        elif op == 'write':
            ok = op_write(args)
        elif op == 'verify':
            ok = op_verify_write(args)
        else:
            raise ValueError(f"unknown operation {op}")
    finally:
        # cleanup: only delete if we created the file in this run
        if not args.keep_file:
            if (not preexisted) and os.path.exists(args.file):
                try:
                    os.remove(args.file)
                    print(f"[cleanup] deleted created file: {args.file}")
                except Exception as e:
                    print(f"[cleanup] failed to delete: {e}")
            else:
                # never delete pre-existing file
                pass

    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
