#!/bin/bash
set -euo pipefail
# --- 默认值设置 ---
# DEFAULT_KERDIR=$BASE/f2fs_head
# DEFAULT_MEM="8184M"
# --- 1. 加载var.sh（核心：读取UUID和基础路径）---
VAR_FILE="./.vars.sh"  # var.sh的路径，根据实际位置调整（比如绝对路径/root/var.sh）
if [[ ! -f "${VAR_FILE}" ]]; then
    echo "错误：找不到变量配置文件 ${VAR_FILE}，请先创建并维护该文件！"
    exit 1
fi
# 加载var.sh并检查加载是否成功
source "${VAR_FILE}" || {
    echo "错误：加载 ${VAR_FILE} 失败！"
    exit 1
}

# --- 初始化变量为默认值 ---
KERDIR="$DEFAULT_KERDIR"
MEM="$DEFAULT_MEM"

# --- 使用说明函数 ---
usage() {
    echo "QEMU 启动脚本"
    echo ""
    echo "用法: $0 [选项]"
    echo ""
    echo "选项:"
    echo "  -k, --kerdir DIR    指定内核所在的目录 (默认: ${DEFAULT_KERDIR})"
    echo "  -m, --mem SIZE      指定分配给虚拟机的内存大小 (默认: ${DEFAULT_MEM})"
    echo "  -h, --help          显示此帮助信息并退出"
    echo ""
    echo "示例:"
    echo "  # 使用默认设置启动"
    echo "  $0"
    echo ""
    echo "  # 使用 'linux_next' 目录下的内核启动"
    echo "  $0 --kerdir linux_next"
    echo ""
    echo "  # 分配 16G 内存启动"
    echo "  $0 -m 16G"
    echo ""
    echo "  # 同时指定内核目录和内存"
    echo "  $0 -k my_kernel_build -m 4G"
}

# --- 解析命令行参数 ---
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        -k|--kerdir)
        KERDIR="$2"
        shift # 移过参数名
        shift # 移过参数值
        ;;
        -m|--mem)
        MEM="$2"
        shift # 移过参数名
        shift # 移过参数值
        ;;
        -h|--help)
        usage
        exit 0
        ;;
        *)    # 未知选项
        echo "错误: 未知选项 '$1'"
        usage
        exit 1
        ;;
    esac
done

# --- 打印最终使用的配置 ---
echo "================ QEMU 配置 ================"
echo "内核目录 (Kernel Directory): ${KERDIR}"
echo "内存大小 (Memory Size)     : ${MEM}"
echo "==========================================="
# --- 执行 QEMU 命令 ---
# 使用 exec 会让 qemu 进程替换掉当前的 shell 进程，是一种好的实践
qemu-system-aarch64 \
    -smp 8 \
    -machine virt,virtualization=true,gic-version=3 \
    -nographic \
    -m size=${MEM} \
    -mem-prealloc \
    -cpu cortex-a72 \
    -kernel ${KERDIR}/arch/arm64/boot/Image \
    -netdev user,id=eth0,hostfwd=tcp::5022-:22,hostfwd=tcp::5080-:80 -device virtio-net-device,netdev=eth0 \
    -drive format=raw,file=$IMG_BASE/ubuntu.img,if=virtio,id=rootdisk \
    -drive format=raw,file=$IMG_BASE/f2fs.img,if=virtio,id=f2fsnorm \
    -virtfs local,path=$SCRIPT/shared_with_qemu,mount_tag=hostshare,security_model=passthrough,id=hostshare \
    -append "panic=5 noinitrd root=/dev/vda rw console=ttyAMA0 nokaslr loglevel=8 ramoops.mem_address=0x1FF800000 ramoops.mem_size=0x200000 ramoops.record_size=0x20000 ramoops.console_size=0x20000 panic_on_oops=1 sysrq_always_enabled" \
    -s | tee guest_console.log
    # -drive format=raw,file=smallf2fs.img,if=virtio,id=smallf2fsdisk \

# -drive format=raw,file=xfs.img,if=virtio,id=xfsdisk \
