#!/bin/bash
# --- 基础配置 (可以根据需要修改) ---
# 基准镜像 (只读)，请确保这些文件存在
BASE_ROOT_IMG="/media/tower/DATA/ubuntu_2404.img"
BASE_F2FS_IMG="/usr/local/share/lff2fs.img"
# --- 默认值设置 ---
DEFAULT_KERDIR="/media/tower/DATA/f2fs_head"  # 默认内核目录
DEFAULT_MEM="4G"
# 宿主机共享目录的基准路径
SHARE=/usr/local/share
# 实例文件存放目录
INSTANCE_DIR=$SHARE/qemu_instances
# --- 初始化变量为默认值 ---
KERDIR="$DEFAULT_KERDIR"
MEM="$DEFAULT_MEM"
# --- 主逻辑 --- 因为涉及到了多个虚拟机示例了,我们对每个虚拟机示例都得知道要进行的操作,并且需要指定
# 唯一的实例名
COMMAND=$1
INSTANCE_NAME=$2

# 检查命令和实例名
if [[ "$COMMAND" != "start" && "$COMMAND" != "stop" && "$COMMAND" != "cleanup" ]] || [[ -z "$INSTANCE_NAME" ]] || [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    usage
    exit 0
fi

# 实例相关路径
INSTANCE_PATH="${INSTANCE_DIR}/${INSTANCE_NAME}"
INSTANCE_ROOT_IMG="${INSTANCE_PATH}/root.qcow2"
INSTANCE_F2FS_IMG="${INSTANCE_PATH}/f2fs.qcow2"
INSTANCE_SHARED_DIR="${INSTANCE_PATH}/shared_with_qemu"
INSTANCE_MOD_DIR="${INSTANCE_PATH}/modshare"
PID_FILE="${INSTANCE_PATH}/qemu.pid"
# --- 使用说明函数 ---
usage() {
    echo "QEMU 实例管理脚本"
    echo ""
    echo "用法: $0 <命令> [实例名] [选项]"
    echo ""
    echo "命令:"
    echo "  start <实例名>   启动一个新的或已存在的虚拟机实例"
    echo "  stop <实例名>    停止一个正在运行的虚拟机实例"
    echo "  cleanup <实例名> 停止实例并删除其所有文件 (差分镜像、共享目录等)"
    echo ""
    echo "选项 (仅用于 'start' 命令):"
    echo "  -k, --kerdir DIR    指定内核所在的目录 (默认: ${DEFAULT_KERDIR})"
    echo "  -m, --mem SIZE      指定分配给虚拟机的内存大小 (默认: ${DEFAULT_MEM})"
    echo "  -h, --help          显示此帮助信息并退出"
    echo ""
    echo "示例:"
    echo "  # 启动名为 'vm1' 的实例 (首次启动会自动创建文件)"
    echo "  $0 start vm1"
    echo ""
    echo "  # 启动 'vm2'，并分配 16G 内存，使用 'linux_next' 内核"
    echo "  $0 start vm2 -m 16G -k linux_next"
    echo ""
    echo "  # 停止 'vm1'"
    echo "  $0 stop vm1"
    echo ""
    echo "  # 彻底清理 'vm2' (删除所有相关文件)"
    echo "  $0 cleanup vm2"
}

# --- 命令处理 ---
case $COMMAND in
    start)
        # --- 解析 start 命令的额外参数 ---
        shift 2 # 移过 command 和 instance_name
        KERDIR="$DEFAULT_KERDIR"
        MEM="$DEFAULT_MEM"
        while [[ $# -gt 0 ]]; do
            key="$1"
            case $key in
                -k|--kerdir) KERDIR="$2"; shift 2 ;;
                -m|--mem) MEM="$2"; shift 2 ;;
                *) echo "错误: 'start' 命令的未知选项 '$1'"; usage; exit 1 ;;
            esac
        done

        # --- 检查基准镜像是否存在 ---
        if [ ! -f "$BASE_ROOT_IMG" ] || [ ! -f "$BASE_F2FS_IMG" ]; then
            echo "错误: 找不到基准镜像 '$BASE_ROOT_IMG' 或 '$BASE_F2FS_IMG'。"
            echo "请将它们放在脚本所在目录，或修改脚本中的 BASE_*_IMG 变量。"
            exit 1
        fi

        # --- 准备实例环境 (如果不存在) ---
        if [ ! -d "$INSTANCE_PATH" ]; then
            echo "首次启动实例 '${INSTANCE_NAME}'，正在创建所需文件..."
            mkdir -p "$INSTANCE_PATH"
            mkdir -p "$INSTANCE_SHARED_DIR"
            mkdir -p "$INSTANCE_MOD_DIR"
            echo "复制基准共享目录文件到实例共享目录..."
            cp -r $SHARE/shared_with_qemu $INSTANCE_SHARED_DIR
            cp -r $SHARE/modshare $INSTANCE_SHARED_DIR
            echo "创建 CoW 根文件系统镜像..."
            qemu-img create -f qcow2 -b "${BASE_ROOT_IMG}" -F raw "$INSTANCE_ROOT_IMG"
            
            echo "创建 CoW f2fs 数据盘镜像..."
            qemu-img create -f qcow2 -b "${BASE_F2FS_IMG}" -F raw "$INSTANCE_F2FS_IMG"
            
            echo "环境准备完毕。"
        fi

        if [ -f "$PID_FILE" ] && ps -p $(cat "$PID_FILE") > /dev/null; then
            echo "错误: 实例 '${INSTANCE_NAME}' 似乎已在运行 (PID: $(cat "$PID_FILE"))。"
            exit 1
        fi
        
        # --- 打印最终配置并启动 QEMU ---
        echo "================ QEMU 配置 (${INSTANCE_NAME}) ================"
        echo "内核目录: ${KERDIR}"
        echo "内存大小: ${MEM}"
        echo "根文件系统: ${INSTANCE_ROOT_IMG} (基于 ${BASE_ROOT_IMG})"
        echo "F2FS 数据盘: ${INSTANCE_F2FS_IMG} (基于 ${BASE_F2FS_IMG})"
        echo "共享目录 1: ${INSTANCE_SHARED_DIR}"
        echo "共享目录 2: ${INSTANCE_MOD_DIR}"
        echo "PID 文件: ${PID_FILE}"
        echo "========================================================"
        
        # 使用 exec 会让 qemu 进程替换掉当前的 shell 进程
        qemu-system-aarch64 \
            -smp 8 \
            -machine virt,virtualization=true,gic-version=3 \
            -nographic \
            -m size=${MEM} \
            -mem-prealloc \
            -cpu cortex-a72 \
            -kernel ${KERDIR}/arch/arm64/boot/Image \
            -netdev user,id=eth0 -device virtio-net-device,netdev=eth0 \
            -drive format=qcow2,file=${INSTANCE_ROOT_IMG},if=virtio,id=rootdisk \
            -drive format=qcow2,file=${INSTANCE_F2FS_IMG},if=virtio,id=f2fsdisk \
            -virtfs local,path=${INSTANCE_SHARED_DIR},mount_tag=hostshare,security_model=passthrough,id=hostshare \
            -virtfs local,path=${INSTANCE_MOD_DIR},mount_tag=modshare,security_model=passthrough,id=modshare \
            -append "noinitrd root=/dev/vda rw console=ttyAMA0 nokaslr loglevel=8 crashkernel=auto sysrq_always_enabled" \
            -pidfile ${PID_FILE} \
            -s
        ;;

    stop)
        if [ ! -f "$PID_FILE" ]; then
            echo "错误: 找不到实例 '${INSTANCE_NAME}' 的 PID 文件。它可能未在运行或已被清理。"
            exit 1
        fi
        PID=$(cat "$PID_FILE")
        echo "正在停止实例 '${INSTANCE_NAME}' (PID: ${PID})..."
        if ps -p $PID > /dev/null; then
            kill $PID
            # 等待进程结束
            while ps -p $PID > /dev/null; do
                sleep 1
            done
            echo "实例已停止。"
            rm -f "$PID_FILE"
        else
            echo "警告: PID ${PID} 不存在。可能实例已经关闭。正在清理 PID 文件。"
            rm -f "$PID_FILE"
        fi
        ;;

    cleanup)
        echo "警告: 这将永久删除实例 '${INSTANCE_NAME}' 的所有数据！"
        read -p "包括: ${INSTANCE_PATH} 目录及其所有内容。确定吗? (y/N) " confirm
        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo "操作已取消。"
            exit 0
        fi
        
        # 首先尝试正常停止
        if [ -f "$PID_FILE" ]; then
            $0 stop "$INSTANCE_NAME"
        fi

        echo "正在删除实例目录: ${INSTANCE_PATH}"
        rm -rf "$INSTANCE_PATH"
        echo "实例 '${INSTANCE_NAME}' 已被彻底清理。"
        ;;
esac
    
    # -drive format=raw,file=smallf2fs.img,if=virtio,id=smallf2fsdisk \

# -drive format=raw,file=xfs.img,if=virtio,id=xfsdisk 
# kasan_multi_shot=1 page_owner=on lockdep_debug=1 " 
