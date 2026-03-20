#!/usr/bin/env python3
import subprocess
import argparse
import psutil
import sys
import math
from pathlib import Path

DEFAULT_FILE = "/mnt/f2fs/lf.c"
DEFAULT_QD_SET = [1]
# --- 测试矩阵定义 ---
BLOCK_SIZES = ["1M","10M","100M","4k","128k"]
ITERATIONS = 1 # 每组测试运行10次

# --- 脚本配置 ---
FIO_SCRIPT_PATH = "./fio_test.sh"
RESULTS_DIR = "fio_results"
# --- 全局变量，用于保存原始内核参数 ---
original_vm_settings = {} # <--- 关键修复：在这里初始化全局字典
def get_mount_point(target: str) -> str:
    """返回目标文件所在的 mount point，依赖 findmnt(8)。"""
    return subprocess.check_output(
        ["findmnt", "-no", "TARGET", "--target", target], text=True
    ).strip()
def remount(target_mp: str, mode: str):
    """mode 取 'ro' 或 'rw'。始终带 noatime、nodiratime 避免 atime update。"""
    opts = f"remount,{mode},noatime,nodiratime"
    print(f"  [mount] sudo mount -o {opts} {target_mp}")
    subprocess.run(["sudo", "mount", "-o", opts, target_mp], check=True)

def set_write_fence():
    """
    为整个测试套件设置“写回屏障”。
    保存原始值，然后设置能阻止脏页回写的参数。
    """
    global original_vm_settings
    print("\n--- [FENCE UP] Setting kernel parameters to prevent writeback... ---")
    params_to_change = {
        "vm.dirty_ratio": "100",
        "vm.dirty_background_ratio": "100",
        "vm.dirty_writeback_centisecs": "0"
    }
    try:
        # 1. 保存原始值
        for param in params_to_change.keys():
            result = subprocess.run(["sysctl", "-n", param], capture_output=True, text=True, check=True)
            original_vm_settings[param] = result.stdout.strip()
        print(f"  Original settings saved: {original_vm_settings}")

        # 2. 设置新值
        for param, value in params_to_change.items():
            print(f"  Setting {param} = {value}")
            subprocess.run(["sudo", "sysctl", "-w", f"{param}={value}"], check=True, capture_output=True)

        print("--- Kernel writeback fence is ACTIVE. ---")

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[!!!] CRITICAL ERROR: Failed to set kernel parameters: {e}", file=sys.stderr)
        print("[!!!] Aborting tests.", file=sys.stderr)
        restore_write_fence()
        sys.exit(1)


def restore_write_fence():
    """
    恢复原始的内核脏页回写参数。
    """
    global original_vm_settings
    if not original_vm_settings:
        return

    print("\n--- [FENCE DOWN] Restoring original kernel parameters... ---")
    try:
        for param, value in original_vm_settings.items():
            print(f"  Restoring {param} = {value}")
            subprocess.run(["sudo", "sysctl", "-w", f"{param}={value}"], check=True, capture_output=True)
        print("--- Kernel parameters restored successfully. ---")
        original_vm_settings = {}
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[!!!] CRITICAL ERROR: Failed to restore kernel parameters: {e}", file=sys.stderr)
        print("[!!!] Please restore them manually:", file=sys.stderr)
        for param, value in original_vm_settings.items():
            print(f"  sudo sysctl -w {param}={value}", file=sys.stderr)
        sys.exit(1)

def get_system_memory_gb():
    """获取系统总内存，并以GB为单位返回一个整数标签"""
    mem_bytes = psutil.virtual_memory().total
    gb_rounded = math.ceil(mem_bytes / (1024**3))
    return f"{gb_rounded}G"

def run_single_test(kernel_ver, mem_config, rw_mode, bs, file_path, ioengine, qd):
    """
    qd: 对 psync 映射为 numjobs；对 io_uring 映射为 iodepth
    """
    if ioengine == "psync":
        numjobs = qd
        iodepth = 1
    else:
        numjobs = 1
        iodepth = qd

    print(f"  -> Running: bs={bs}, mode={rw_mode}, kernel={kernel_ver}, mem={mem_config}, "
          f"file={file_path}, engine={ioengine}, qd={qd} (iodepth={iodepth}, numjobs={numjobs})")

    title = f"{kernel_ver}_{mem_config}_mem"
    extra_fio_flags = []
    if rw_mode == 'r':
        extra_fio_flags += ["--readonly=1",
                            "--allow_file_create=0",
                            "--unlink=0"]
    try:
        subprocess.run(
            [
                "sudo", "bash", FIO_SCRIPT_PATH,
                title, bs, rw_mode,
                f"file={file_path}",
                f"ioengine={ioengine}",
                f"iodepth={iodepth}",
                f"numjobs={numjobs}",
                f"qd={qd}",
                *extra_fio_flags,         # <--- 直接拼到参数列表
            ],
            check=True,
            capture_output=False,
            text=True
        )
    except subprocess.CalledProcessError as e:
        print(f"  [!] ERROR running test for bs={bs}, qd={qd}.")
        print(f"  [!] STDERR: {e.stderr}")
        return False
    return True

def main():
    parser = argparse.ArgumentParser(description="Automated FIO benchmark orchestrator.")
    parser.add_argument("--name",'-n', required=True, help="Kernel version identifier (e.g., 'vanilla', 'iomap_v1').")
    parser.add_argument("--type", "-t",required=True, choices=['read','write','sync','r','w','s'],  help="Test type: read/write/sync.")
    parser.add_argument("--file", "-f", default=DEFAULT_FILE, help=f"Target file for FIO (default: {DEFAULT_FILE})")
    parser.add_argument("--ioengine","-e", choices=["psync","io_uring","libaio"], default="psync", help="fio ioengine.")
    parser.add_argument("--qdset", default="1", help="Comma-separated queue depths to sweep.")
    parser.add_argument("--qdwrite", action="store_true", help="Also sweep queue depths for WRITE tests.")
    args = parser.parse_args()

    try:
        qd_list = [int(x) for x in args.qdset.split(",") if x.strip()]
    except ValueError:
        raise SystemExit("Invalid --qdset. Use like: 1,2,4,8,16")

    mem_config = get_system_memory_gb()
    print(f"--- Detected System Memory: {mem_config} ---")
    Path(RESULTS_DIR).mkdir(exist_ok=True)

    need_fence = args.type in ['w', 'write','r','read']

    try:
        if need_fence:
            set_write_fence()

        print(f"\n=== Starting Benchmark for Kernel: {args.name}, Type: {args.type} ===")

        if args.type in['read','r']:
            mount_point = get_mount_point(args.file)
            remount(mount_point, "ro")             # -------- ro,noatime ----------
            print("Running COLD READ tests (cache cleared before each run)...")
            for bs in BLOCK_SIZES:
                print(f"\n[+] Testing Block Size: {bs}")
                for qd in qd_list:
                    for i in range(ITERATIONS):
                        print(f"  - Iteration {i+1}/{ITERATIONS}")
                        run_single_test(args.name, mem_config, 'r', bs, args.file,args.ioengine, qd)
            remount(mount_point, "rw")
        elif args.type in['w','write']:
            print("Running HOT WRITE tests (on cached file)...")
            print("\n[+] Preparing cached file by performing an initial write...")
            if not run_single_test(args.name+"_cold", mem_config, 'w', '1M', args.file, args.ioengine, qd_list[0]):
                 print("[!] Failed to prepare cached file. Aborting.")
                 return

            sweep_list = qd_list if args.qdwrite else [qd_list[0]]
            for bs in BLOCK_SIZES:
                print(f"\n[+] Testing Block Size: {bs}")
                for qd in sweep_list:
                    for i in range(ITERATIONS):
                        print(f"  - Iteration {i+1}/{ITERATIONS}")
                        run_single_test(args.name, mem_config, 'w', bs, args.file, args.ioengine, qd)

        elif args.type in ['sync','s']:
            print("Running SYNC WRITE tests (fsync after every write)...")
            sweep_list = qd_list if args.qdwrite else [qd_list[0]]
            for bs in BLOCK_SIZES:
                print(f"\n[+] Testing Block Size: {bs}")
                for qd in sweep_list:
                    for i in range(ITERATIONS):
                        print(f"  - Iteration {i+1}/{ITERATIONS}")
                        run_single_test(args.name, mem_config, 's', bs, args.file, args.ioengine, qd)

    finally:
        if need_fence:
            restore_write_fence()

    print("\n=== Benchmark Finished! ===")
    print(f"All results are in the '{RESULTS_DIR}' directory.")

if __name__ == "__main__":
    main()
