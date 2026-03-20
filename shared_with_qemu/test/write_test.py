import os
import time # 用于演示和可能的观察点

filename = "/mnt/f2fs/com.txt" # 请确保这个路径和文件对你来说是可写的
data_to_write = b"NEW_PYTHON_DATA_FSYNCED"
offset = 10 # 字节偏移量

# --- 前置准备：确保文件存在且有足够内容，方便观察变化 ---
# 如果文件不存在或太小，创建一个或覆盖它
# 你可能需要根据你的追踪设置调整这部分
initial_content_size = 50
if not os.path.exists(filename) or os.path.getsize(filename) < offset + len(data_to_write) :
    print(f"Initializing '{filename}' for the test...")
    with open(filename, "wb") as f_init:
        f_init.write(b'0123456789' * (initial_content_size // 10)) # 写入一些初始数据
    print(f"'{filename}' initialized with {os.path.getsize(filename)} bytes.")
else:
    print(f"'{filename}' already exists with {os.path.getsize(filename)} bytes. Proceeding with test.")
print("-" * 30)

try:
    print(f"Step 1: Opening '{filename}' in 'r+b' mode.")
    with open(filename, "r+b") as f:
        print(f"Step 2: Seeking to offset {offset}.")
        f.seek(offset)

        print(f"Step 3: Writing data: {data_to_write!r} (length: {len(data_to_write)})")
        bytes_written = f.write(data_to_write)
        print(f"   {bytes_written} bytes written to Python's internal buffer.")

        print("Step 4: Calling f.flush() to push data to OS page cache.")
        print("        This should mark the corresponding pages in memory as 'dirty'.")
        f.flush()
        print("   f.flush() completed.")

        # 在这里，你可以暂停或设置断点，使用外部工具（如 /proc/meminfo 看 Dirty: 行，
        # 或更专门的工具如 `perf`, `ftrace` 的探针）来观察脏页状态，但这可能很短暂。

        print(f"Step 5: Calling os.fsync({f.fileno()}) to force write-back of dirty pages to disk.")
        # 获取文件描述符 f.fileno() 给 os.fsync()
        start_sync_time = time.perf_counter()
        os.fsync(f.fileno())
        end_sync_time = time.perf_counter()
        print(f"   os.fsync() completed in {end_sync_time - start_sync_time:.6f} seconds.")
        print("        Dirty pages for this file should now be physically written to disk.")

    print(f"Step 6: File '{filename}' closed (implicitly by 'with' statement).")
    print("-" * 30)
    print("Test finished. You can now verify the file content on disk.")
    print("And analyze traces collected by your tracing tools during script execution.")


except FileNotFoundError:
    print(f"Error: File '{filename}' not found. Please ensure the path is correct and you have permissions.")
except PermissionError:
    print(f"Error: Permission denied for '{filename}'. Check file/directory permissions.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
    import traceback
    traceback.print_exc()

# --- 后置验证 (可选) ---
try:
    with open(filename, "rb") as f_verify:
        f_verify.seek(offset)
        read_back_data = f_verify.read(len(data_to_write))
        if read_back_data == data_to_write:
            print(f"\nVerification: Data successfully written and read back from '{filename}'.")
            print(f"Expected: {data_to_write!r}")
            print(f"Got:      {read_back_data!r}")
        else:
            print(f"\nVerification FAILED for '{filename}'.")
            print(f"Expected: {data_to_write!r}")
            print(f"Got:      {read_back_data!r}")
except Exception as e:
    print(f"\nError during verification: {e}")

