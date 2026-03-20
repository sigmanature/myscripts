#!/usr/bin/env python3
import os
import sys
import time
import argparse
import binascii  # For hex output similar to hexdump

# --- Size Parsing Function (Python equivalent of bash function) ---
def parse_bytes(size_str):
   """
   Parses a size string with optional units (k, m, g, b) into bytes.
   Units are case-insensitive. 'b' or no unit means bytes.
   """
   size_str = str(size_str).strip().lower()
   if not size_str:
       raise ValueError("Size string cannot be empty")

   unit = size_str[-1]
   if unit.isalpha():
       num_part = size_str[:-1]
       if unit == 'k':
           factor = 1024
       elif unit == 'm':
           factor = 1024 * 1024
       elif unit == 'g':
           factor = 1024 * 1024 * 1024
       elif unit == 'b':
           factor = 1
       else:
           raise ValueError(f"Invalid unit '{unit}' in '{size_str}'")
   else:
       # No unit or ends with a digit
       num_part = size_str
       factor = 1  # Default to bytes

   if not num_part.isdigit():
       # Check if the original string was just a unit character like 'k'
       if len(size_str) == 1 and size_str.isalpha():
           raise ValueError(f"Missing number before unit in '{size_str}'")
       # Otherwise, invalid number part
       raise ValueError(f"Invalid number part '{num_part}' in '{size_str}'")

   try:
       num = int(num_part)
   except ValueError:
       raise ValueError(f"Could not convert number '{num_part}' to integer in '{size_str}'")

   return num * factor


# --- Hexdump Function (Simplified version) ---
def hex_dump(data, bytes_per_line=16):
   """
   Formats binary data into a hex dump string similar to hexdump -C.
   """
   lines = []
   for i in range(0, len(data), bytes_per_line):
       chunk = data[i:i + bytes_per_line]
       hex_part = ' '.join(f'{b:02x}' for b in chunk)
       hex_part = hex_part.ljust(bytes_per_line * 3 - 1)
       ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
       lines.append(f"{i:08x}  {hex_part}  |{ascii_part}|")
   return '\n'.join(lines)

def dump_aligned_blocks(filename, offset_bytes, size_bytes, block_size=4096, max_dump_bytes=64*1024, title=""):
    """
    Dump blocks aligned to block_size that intersect [offset_bytes, offset_bytes + size_bytes).
    - block_size default 4096
    - max_dump_bytes limits output to avoid terminal explosion
    """
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
    print(f"write_range =[{offset_bytes}, {offset_bytes + size_bytes})")
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

    # Print hexdump with base offset shown as aligned start
    # hex_dump() prints offsets starting at 0; we remap by prefixing line offsets.
    # Simple way: reuse hex_dump and just tell the user the base.
    print(f"(Base offset shown below is relative; add {start} to get file offsets)")
    print(hex_dump(data))
    print("=" * 70 + "\n")
# --- Helpers for verification / pattern generation ---
def generate_write_pattern(size_bytes: int) -> bytes:
   """Generate the same write pattern used by perform_write()."""
   if size_bytes <= 0:
       return b""
   pattern = b"PyWrtDta"
   full_pattern = (pattern * (size_bytes // len(pattern) + 1))
   return full_pattern[:size_bytes]


def clamp_read_window(start: int, end: int, file_size: int):
   """Clamp a [start, end) range into file bounds."""
   start = max(0, start)
   end = min(file_size, end)
   if end < start:
       end = start
   return start, end


# --- Main Operation Functions ---
def perform_write(filename, offset_bytes, size_bytes, keep_file):
   """
   Performs the write operation: write data, flush, fsync.
   Initializes the file if it doesn't exist or is too small.
   """
   print(f"--- Write Operation ---")
   print(f"Target File: {filename}")
   print(f"Offset: {offset_bytes} bytes")
   print(f"Size: {size_bytes} bytes")
   print("-" * 30)

   data_to_write = generate_write_pattern(size_bytes)

   # --- Pre-check and Initialization ---
   required_size = offset_bytes + size_bytes
   initialized = False
   if not os.path.exists(filename) or os.path.getsize(filename) < required_size:
       print(f"Initializing '{filename}' to at least {required_size} bytes...")
       try:
           os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
           with open(filename, "wb") as f_init:
               if required_size > 0:
                   f_init.seek(required_size - 1)
                   f_init.write(b'\x00')
               else:
                   pass
           print(f"'{filename}' initialized/resized to {os.path.getsize(filename)} bytes.")
           initialized = True
       except Exception as e:
           print(f"Error during file initialization: {e}")
           return False

   if not initialized:
       print(f"'{filename}' already exists with {os.path.getsize(filename)} bytes. Proceeding.")

   print("-" * 30)

   try:
       print(f"Step 1: Opening '{filename}' in 'r+b' mode.")
       with open(filename, "r+b") as f:
           print(f"Step 2: Seeking to offset {offset_bytes}.")
           f.seek(offset_bytes)

           print(f"Step 3: Writing data:  (length: {len(data_to_write)})")
           bytes_written = f.write(data_to_write)
           print(f"   {bytes_written} bytes passed to write() call.")
           if bytes_written != len(data_to_write):
               print(f"   Warning: Expected to write {len(data_to_write)} bytes, but write() returned {bytes_written}")

           print("Step 4: Calling f.flush() to push data to OS page cache.")
           f.flush()
           print("   f.flush() completed.")

           # print(f"Step 5: Calling os.fsync({f.fileno()}) to force write-back to disk.")
           # start_sync_time = time.perf_counter()
           # os.fsync(f.fileno())
           # end_sync_time = time.perf_counter()
           # print(f"   os.fsync() completed in {end_sync_time - start_sync_time:.6f} seconds.")

       print(f"Step 6: File '{filename}' closed.")
       print("-" * 30)
       print("Write operation finished.")
       return True

   except FileNotFoundError:
       print(f"Error: File '{filename}' not found unexpectedly after check/init.")
       return False
   except PermissionError:
       print(f"Error: Permission denied for '{filename}'. Check permissions.")
       return False
   except OSError as e:
       print(f"An OS error occurred during file operation: {e}")
       return False
   except Exception as e:
       print(f"An unexpected error occurred during write: {e}")
       import traceback
       traceback.print_exc()
       return False


def perform_read(filename, offset_bytes, size_bytes, keep_file):
   """
   Performs the read operation.
   - If the file exists, it reads from it directly.
   - If the file does NOT exist, it creates a new zero-filled file and then reads from it.
   """
   print(f"--- Read Operation ---")
   print(f"Target File: {filename}")
   print(f"Offset: {offset_bytes} bytes")
   print(f"Size: {size_bytes} bytes")
   print("-" * 30)

   # 1. 检查文件是否存在
   if not os.path.exists(filename):
       # --- 文件不存在：执行创建逻辑 ---
       print(f"File '{filename}' does not exist. Creating it first.")

       file_size_to_create = max(offset_bytes + size_bytes, size_bytes * 10)
       if size_bytes == 0 and offset_bytes > 0:
           file_size_to_create = max(file_size_to_create, offset_bytes)
       if file_size_to_create < 0:
           file_size_to_create = 0

       print(f"Creating file with size {file_size_to_create} bytes (filled with zeros)...")
       try:
           os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
           with open(filename, "wb") as f_create:
               if file_size_to_create > 0:
                   f_create.seek(file_size_to_create - 1)
                   f_create.write(b'\x00')
           print(f"File '{filename}' created with size {os.path.getsize(filename)} bytes.")
       except Exception as e:
           print(f"Error during file creation: {e}")
           return False
   else:
       print(f"File '{filename}' already exists (size: {os.path.getsize(filename)} bytes). Proceeding to read.")

   print("-" * 30)

   # 2. 执行读取操作
   if size_bytes <= 0:
       print("Read size is 0. No data to display.")
       return True

   try:
       print(f"Reading {size_bytes} bytes from offset {offset_bytes}...")
       with open(filename, "rb") as f_read:
           file_size = os.fstat(f_read.fileno()).st_size
           if file_size < offset_bytes + size_bytes:
               print(f"Warning: The file size ({file_size} bytes) is smaller than the requested read area (up to {offset_bytes + size_bytes} bytes).")
               print("         The read may be partial or return empty data.")

           f_read.seek(offset_bytes)
           read_data = f_read.read(size_bytes)

       print(f"Read {len(read_data)} bytes. Displaying hex dump:")
       print("-" * 30)
       if read_data:
           # print(hex_dump(read_data))
           print("read data:sucessfull")
           pass
       else:
           print("(No data read or empty data)")
       print("-" * 30)
       print("Read operation finished.")
       return True

   except FileNotFoundError:
       print(f"Error: File '{filename}' not found unexpectedly.")
       return False
   except PermissionError:
       print(f"Error: Permission denied for '{filename}'. Check permissions.")
       return False
   except OSError as e:
       print(f"An OS error occurred during file read: {e}")
       return False
   except Exception as e:
       print(f"An unexpected error occurred during read: {e}")
       import traceback
       traceback.print_exc()
       return False


def perform_verify_write(filename, offset_bytes, size_bytes, keep_file):
   """
   Verify write correctness.

   Rules:
     - If file does NOT exist: create it (zero-filled / sparse), then write+verify.
     - If file exists: exit (fail) and MUST NOT delete that file.
     - size_bytes must be <= 128 KiB.

   Verification:
     - Compare written region with expected pattern.
     - Sample-check surrounding bytes remain zero (helps offset/block misalignment cases).
   """
   print(f"--- Verify-Write Operation ---")
   print(f"Target File: {filename}")
   print(f"Offset: {offset_bytes} bytes")
   print(f"Size: {size_bytes} bytes")
   print("-" * 30)

   MAX_VERIFY = 128 * 1024
   if size_bytes < 0 or offset_bytes < 0:
       print("Error: Offset/size cannot be negative.")
       return False
   if size_bytes > MAX_VERIFY:
       print(f"Error: verify 模式下写入量不能超过 128K（当前: {size_bytes}）。")
       return False

   # If file exists -> exit
   if os.path.exists(filename):
       print(f"Error: File '{filename}' already exists. verify 模式要求文件不存在，避免误伤。")
       return False

   # Create file (zero-filled, sparse)
   # Add tail padding so we can sample-check after the write area.
   tail_padding = 4096
   required_size = offset_bytes + size_bytes + tail_padding
   if required_size < 0:
       required_size = 0

   print(f"Creating new file '{filename}' with size {required_size} bytes (zero-filled/sparse)...")
   try:
       os.makedirs(os.path.dirname(filename) or '.', exist_ok=True)
       with open(filename, "wb") as f_create:
           if required_size > 0:
               # Sparse zero file (0x00). Fast for huge offsets.
               f_create.seek(required_size - 1)
               f_create.write(b'\x00')

               # 如果你真想“字符0”(ASCII '0' = 0x30) 填满整个文件，
               # 需要真实写满每个字节，会很慢且大 offset 会非常重：
               # f_create.seek(0)
               # chunk = b'0' * 4096
               # remaining = required_size
               # while remaining > 0:
               #     n = min(remaining, len(chunk))
               #     f_create.write(chunk[:n])
               #     remaining -= n
       print(f"File created. Size now: {os.path.getsize(filename)} bytes.")
   except Exception as e:
       print(f"Error during file creation: {e}")
       return False

   expected = generate_write_pattern(size_bytes)

   # Read before write: target region should be zeros (for sparse 0x00 file)
   try:
       with open(filename, "rb") as f:
           f.seek(offset_bytes)
           before = f.read(size_bytes)
   except Exception as e:
       print(f"Error reading before-write data: {e}")
       return False

   if size_bytes > 0 and before and any(b != 0x00 for b in before):
       print("Warning: 新建文件写入区域在写之前不是全 0x00（理论上应全 0x00）。")
   print("Pre-check: read-before-write done.")

   # Perform the write
   try:
       with open(filename, "r+b") as f:
           f.seek(offset_bytes)
           bw = f.write(expected)
           f.flush()
           # 如果你想验证 fsync 情况，把下面打开：
           # os.fsync(f.fileno())
       if bw != len(expected):
           print(f"Warning: write() returned {bw}, expected {len(expected)}.")
   except Exception as e:
       print(f"Error during verify write: {e}")
       return False
   dump_aligned_blocks(filename, offset_bytes, size_bytes, block_size=4096, max_dump_bytes=64*1024, title="BEFORE write")
   # Verify after write
   try:
       with open(filename, "rb") as f:
           file_size = os.fstat(f.fileno()).st_size

           # Read exact written region
           f.seek(offset_bytes)
           after = f.read(size_bytes)

           # Sample windows around write region
           window = 256
           pre_s, pre_e = clamp_read_window(offset_bytes - window, offset_bytes, file_size)
           post_s, post_e = clamp_read_window(offset_bytes + size_bytes, offset_bytes + size_bytes + window, file_size)

           f.seek(pre_s)
           pre_region = f.read(pre_e - pre_s)

           f.seek(post_s)
           post_region = f.read(post_e - post_s)

   except Exception as e:
       print(f"Error during verify read: {e}")
       return False

   dump_aligned_blocks(filename, offset_bytes, size_bytes, block_size=4096, max_dump_bytes=64*1024, title="AFTER write")
   ok = True

   # 1) Compare written region
   if after != expected:
       ok = False
       print("\n[FAIL] Written region does NOT match expected pattern.")

       mismatches = []
       limit = min(len(after), len(expected))
       for i in range(limit):
           if after[i] != expected[i]:
               mismatches.append((i, expected[i], after[i]))
               if len(mismatches) >= 20:
                   break

       print(f"Mismatches found (showing up to {len(mismatches)}):")
       for i, exp_b, got_b in mismatches:
           print(f"  +{i}  expected=0x{exp_b:02x}  got=0x{got_b:02x}")

       dump_len = min(128, len(expected), len(after))
       print("\nExpected (first 128 bytes):")
       print(hex_dump(expected[:dump_len]))
       print("\nActual (first 128 bytes):")
       print(hex_dump(after[:dump_len]))
   else:
       print("[OK] Written region matches expected pattern.")

   # 2) Check surroundings remain zero
   if any(b != 0x00 for b in pre_region):
       ok = False
       print(f"[FAIL] Bytes BEFORE write-region (sample {len(pre_region)} bytes) are not all 0x00.")
   else:
       print(f"[OK] Bytes BEFORE write-region sample remain 0x00. (sample {len(pre_region)} bytes)")

   if any(b != 0x00 for b in post_region):
       ok = False
       print(f"[FAIL] Bytes AFTER write-region (sample {len(post_region)} bytes) are not all 0x00.")
   else:
       print(f"[OK] Bytes AFTER write-region sample remain 0x00. (sample {len(post_region)} bytes)")

   print("-" * 30)
   print("Verify-write operation finished.")
   return ok


# --- Main Execution ---
if __name__ == "__main__":
   parser = argparse.ArgumentParser(
       description="Perform read or write operations on a file with fsync control.",
       formatter_class=argparse.RawTextHelpFormatter
   )

   parser.add_argument(
       "operation",
       choices=['read', 'write', 'verify', "r", "w", "v"],
       help="The operation to perform: 'read' or 'write' or 'verify'."
   )
   parser.add_argument(
       "offset",
       type=str,
       help="Offset in the file. Supports units: b (bytes, default), k, m, g."
   )
   parser.add_argument(
       "size",
       type=str,
       help="Size of data to read or write. Supports units: b (bytes, default), k, m, g."
   )
   parser.add_argument(
       "-f", "--file",
       default="/mnt/f2fs/com.c",
       help="Path to the target file (default: /mnt/f2fs/com.txt)"
   )
   parser.add_argument(
       "-k", "--keep-file",
       action="store_true",
       help="Do not delete the file after the operation completes."
   )

   args = parser.parse_args()

   # --- Parse offset and size ---
   try:
       offset_bytes = parse_bytes(args.offset)
       size_bytes = parse_bytes(args.size)
       if offset_bytes < 0 or size_bytes < 0:
           raise ValueError("Offset and size cannot be negative.")
   except ValueError as e:
       print(f"Error parsing arguments: {e}", file=sys.stderr)
       parser.print_usage()
       sys.exit(1)

   # Special safety: verify 模式下，若目标文件本来就存在，cleanup 不能删它
   preexist_for_verify = False
   if args.operation in ["verify", "v"]:
       preexist_for_verify = os.path.exists(args.file)

   # --- Execute requested operation ---
   success = False
   try:
       if args.operation in ['write', 'w']:
           success = perform_write(args.file, offset_bytes, size_bytes, args.keep_file)
       elif args.operation in ['read', 'r']:
           success = perform_read(args.file, offset_bytes, size_bytes, args.keep_file)
       elif args.operation in ['verify', 'v']:
           success = perform_verify_write(args.file, offset_bytes, size_bytes, args.keep_file)
   finally:
       # --- Cleanup ---
       if not args.keep_file:
           # verify 模式：如果一开始文件就存在，绝对不删
           if (args.operation in ["verify", "v"]) and preexist_for_verify:
               print(f"\nCleanup: verify 模式检测到文件原本就存在，跳过删除：'{args.file}'")
           else:
               if os.path.exists(args.file):
                   print(f"\nCleaning up: Deleting '{args.file}'...")
                   try:
                       os.remove(args.file)
                       print(f"File '{args.file}' deleted.")
                   except Exception as e:
                       print(f"Error deleting file '{args.file}': {e}")
               else:
                   print(f"\nCleanup: File '{args.file}' not found, no deletion needed.")
       else:
           print(f"\nSkipping cleanup: File '{args.file}' will be kept (--keep-file specified).")

   sys.exit(0 if success else 1)
