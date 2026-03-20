import os
import fcntl
import struct # Needed for complex arguments

# 1. 定义 ioctl 命令码 (必须从 C 头文件 <linux/f2fs.h> 或 <linux/fs.h> 手动查找并复制)
#    注意：这些值可能随内核版本变化！
#    例如 (这些值是示例，你需要查找你系统上对应头文件的实际值):
F2FS_IOC_ABORT_ATOMIC_WRITE=0xf505
F2FS_IOC_GARBAGE_COLLECT = 0x4004f506 # Example value, check <linux/f2fs.h>
F2FS_IOC_MOVE_RANGE=0xc020f509
F2FS_IOC_START_ATOMIC_WRITE=0xf501
# 定义 fstrim_range 结构体的格式 (来自 <linux/fs.h>)
# struct fstrim_range {
#   __u64 start;
#   __u64 len;
#   __u64 minlen;
# };
# 'Q' is unsigned long long (64-bit) in standard sizes
FSTRIM_RANGE_FORMAT = "QQQ"

try:
    # 2. 打开文件描述符 (需要相应权限, 通常是 root)
    #    打开挂载点目录通常就足够了
    fd = os.open("/mnt/f2fs/atomic.txt", os.O_RDWR)

    # 3. 调用 ioctl

    # 示例 1: 调用 F2FS_IOC_GARBAGE_COLLECT (假设它不需要参数或参数为 0)
    # 注意：你需要确认 F2FS_IOC_GARBAGE_COLLECT 的实际参数要求
    try:
        print(f"Attempting F2FS_IOC_START_ATOMIC_WRITE (command code: {F2FS_IOC_GARBAGE_COLLECT:#x})...")
        # 第三个参数 arg=0 表示传递 0
        fcntl.ioctl(fd, F2FS_IOC_START_ATOMIC_WRITE, 0)
        breakpoint()
        print("F2FS_IOC_START_ATOMIC_WRITE succeeded.")
    except OSError as e:
        print(f"F2FS_IOC_START_ATOMIC_WRITE failed: {e}")
    except Exception as e:
         print(f"An unexpected error occurred: {e}")


    # # 示例 2: 调用 FITRIM (需要传递结构体指针)
    # range_data = struct.pack(FSTRIM_RANGE_FORMAT,
    #                          0,        # start = 0
    #                          (1 << 64) - 1, # len = ULLONG_MAX (all)
    #                          0)        # minlen = 0
    # try:
    #     print(f"Attempting FITRIM (command code: {FITRIM:#x})...")
    #     # 第三个参数 arg=range_data 传递打包好的字节数据
    #     # mutable_flag=True 表示内核可能会修改这个缓冲区 (虽然 FITRIM 通常不修改)
    #     fcntl.ioctl(fd, FITRIM, range_data, True)
    #     print("FITRIM succeeded.")
    # except OSError as e:
    #     print(f"FITRIM failed: {e}")
    # except Exception as e:
    #      print(f"An unexpected error occurred: {e}")

    # # 示例 3: 获取文件系统标签 (FS_IOC_GETFSLABEL)
    # # 需要一个足够大的缓冲区来接收标签
    # buffer = bytearray(128) # FS_LABEL_MAX in C is often 128 or 256
    # try:
    #     print(f"Attempting FS_IOC_GETFSLABEL (command code: {FS_IOC_GETFSLABEL:#x})...")
    #     # 第三个参数 arg=buffer 传递一个可变字节数组
    #     fcntl.ioctl(fd, FS_IOC_GETFSLABEL, buffer, True)
    #     # C 字符串以 null 结尾，找到第一个 null 字符
    #     label = buffer[:buffer.find(b'\x00')].decode('utf-8', errors='replace')
    #     print(f"FS Label: {label}")
    # except OSError as e:
    #     print(f"FS_IOC_GETFSLABEL failed: {e}")
    # except Exception as e:
    #      print(f"An unexpected error occurred: {e}")


except OSError as e:
    print(f"Failed to open directory or general OS error: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
finally:
    if 'fd' in locals() and fd >= 0:
        os.close(fd)

