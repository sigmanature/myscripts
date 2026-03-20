#!/bin/bash

# 脚本功能: 运行FIO读/写测试，并将结果保存到唯一的、描述性的文件中。
# 用法: ./fio_test.sh <experiment_title> [block_size] [r|w]
# 示例 1 (默认读, 1M bs):  ./fio_test.sh vanilla_kernel
# 示例 2 (指定读, 4k bs):    ./fio_test.sh vanilla_kernel 4k r
# 示例 3 (指定写, 128k bs):  ./fio_test.sh modified_kernel 128k w

# --- 基础配置 (可以根据需要修改) ---
FIO_TARGET_FILE="/mnt/f2fs/lf.c"
RESULTS_DIR="$HOME/fio_results"
mkdir -p "$RESULTS_DIR"
CLEAR_CACHE_SCRIPT="./clear_cache.sh"
DEFAULT_BS="1M"
DEFAULT_RW_MODE="r" # <--- 默认模式现在是 'r' (read)
IOENGINE="psync"
IODEPTH=1
NUMJOBS=1
USER_QD=""  # 仅用于输出命名
USE_FSYNC_EVERY_IO=0
# --- 全局变量，用于保存原始内核参数 ---
for kv in "${@:4}"; do
  case "$kv" in
    file=*)     FIO_TARGET_FILE="${kv#file=}" ;;
    ioengine=*) IOENGINE="${kv#ioengine=}" ;;
    iodepth=*)  IODEPTH="${kv#iodepth=}" ;;
    numjobs=*)  NUMJOBS="${kv#numjobs=}" ;;
    qd=*)       USER_QD="${kv#qd=}" ;;
    *) echo "警告: 未知参数 $kv" ;;
  esac
done
OLD_DIRTY_RATIO=""
OLD_DIRTY_BG_RATIO=""

# --- 清理函数：无论脚本如何退出，都会被调用以恢复内核参数 ---
function cleanup_and_restore_thresholds {
    echo # 换行
    # 只有当我们成功修改过参数后才需要恢复
    if [ -n "$OLD_DIRTY_RATIO" ]; then
        echo "--- Restoring original kernel dirty page thresholds... ---"
        sudo sysctl -w vm.dirty_ratio="$OLD_DIRTY_RATIO"
        sudo sysctl -w vm.dirty_background_ratio="$OLD_DIRTY_BG_RATIO"
        echo "Kernel parameters restored."
    fi
}

# --- 关键的安全保障：设置一个“陷阱”(trap) ---
# 无论脚本是正常结束(EXIT)，还是被中断(INT)或终止(TERM)，
# 都会自动调用上面的 cleanup_and_restore_thresholds 函数。
trap cleanup_and_restore_thresholds EXIT INT TERM


# 1. 检查用户是否提供了实验标题 (第一个参数)
if [ -z "$1" ]; then
    echo "错误: 请提供一个实验标题作为第一个参数。"
    echo "用法: $0 <experiment_title> [block_size] [r|w]"
    exit 1
fi

# 2. 准备变量
EXPERIMENT_TITLE=$1

# 检查是否提供了块大小 (第二个参数)，否则使用默认值
BS=${2:-$DEFAULT_BS}

# 检查是否提供了读写模式 (第三个参数)，并将其转换为fio能识别的格式
USER_RW_MODE=${3:-$DEFAULT_RW_MODE}
case "$USER_RW_MODE" in
  r|read)
    RW_MODE="read"
    MODE_TAG="read"
    ;;
  w|write)
    RW_MODE="write"
    MODE_TAG="write"
    ;;
  s|sync)
    RW_MODE="write"          # fio 的 rw 仍然是 write
    MODE_TAG="sync"          # 文件名里体现为 sync
    USE_FSYNC_EVERY_IO=1     # 每次 write 后 fsync
    ;;
  *)
    echo "错误: 无效的模式 '${USER_RW_MODE}'。使用 r|w|s (read|write|sync)。"
    exit 1
    ;;
esac
# 3. 如果是写测试，则启动“安全围栏”
#if [ "$RW_MODE" == "write" ]; then
#    echo "--- Write test detected. Setting up 'safe fence'... ---"
    # 保存原始值
#    OLD_DIRTY_RATIO=$(cat /proc/sys/vm/dirty_ratio)
#    OLD_DIRTY_BG_RATIO=$(cat /proc/sys/vm/dirty_background_ratio)
#    echo "Original values: dirty_ratio=${OLD_DIRTY_RATIO}, dirty_background_ratio=${OLD_DIRTY_BG_RATIO}"

    # 设置非常高的临时值
#    echo "Setting high temporary thresholds to prevent writeback..."
#    sudo sysctl -w vm.dirty_background_ratio=90
#    sudo sysctl -w vm.dirty_ratio=90
#fi
TIMESTAMP=$(date +'%Y%m%d-%H%M%S')
# 将读写模式也加入到文件名中！
OUTPUT_FILENAME="${EXPERIMENT_TITLE}_${RW_MODE}_bs-${BS}_${TIMESTAMP}.log"
OUTPUT_FILE_PATH="${RESULTS_DIR}/${OUTPUT_FILENAME}"

# 3. 创建结果目录 (如果不存在)
mkdir -p "${RESULTS_DIR}"

echo "=========================================================="
echo "准备执行FIO测试..."
echo "  - 实验标题: ${EXPERIMENT_TITLE}"
echo "  - 读写模式: ${RW_MODE} (输入: '${USER_RW_MODE}')"
echo "  - 块大小:   ${BS}"
echo "  - 引擎:     ${IOENGINE}"
echo "  - iodepth:  ${IODEPTH}"
echo "  - numjobs:  ${NUMJOBS}"
echo "  - 文件:     ${FIO_TARGET_FILE}"
echo "  - 时间戳:   ${TIMESTAMP}"
echo "  - 输出:     ${OUTPUT_FILE_PATH}"
echo "=========================================================="

# 5. 清理系统缓存 (你可以取消这里的注释来启用它)
echo "正在清理系统缓存..."
if [ "${RW_MODE}" == "read" ]; then
    if [ -f "${CLEAR_CACHE_SCRIPT}" ]; then
    # if [ "${RW_MODE}" == "write" ]; then
        # rm -f "${FIO_TARGET_FILE}"
        # echo "旧的测试文件 ${FIO_TARGET_FILE} 已删除。"
    # fi
        sudo "${CLEAR_CACHE_SCRIPT}"
        vmtouch "${FIO_TARGET_FILE}"
    fi
else
    echo "警告: 缓存清理脚本 ${CLEAR_CACHE_SCRIPT} 未找到，跳过此步骤！"
fi
# sleep 2
# 组装额外 fio 选项
EXTRA_OPTS=()
if [ "${USE_FSYNC_EVERY_IO}" -eq 1 ]; then
  EXTRA_OPTS+=( --fsync=1 )     # 每次写后立刻 fsync；如需 fdatasync 改成 --fdatasync=1
fi
# 6. 执行FIO命令
PERF=/usr/local/bin/perf
PERF_DATA="${RESULTS_DIR}/${EXPERIMENT_TITLE}_${MODE_TAG}_bs-${BS}_${TIMESTAMP}.data"
echo "开始运行FIO..."
ulimit -l unlimited          # 也可写在 /etc/security/limits.conf
$PERF record -o "$PERF_DATA"          \
             -e cycles                \
             -g --call-graph dwarf    \
             --                       \
            fio --name="buffered-${RW_MODE}" \
              --filename="${FIO_TARGET_FILE}" \
              --rw="${RW_MODE}" \
              --bs="${BS}" \
              --io_size=512M \
              --ioengine="${IOENGINE}" \
              --numjobs="${NUMJOBS}" \
              --group_reporting \
              --allow_file_create=0 \
              "${EXTRA_OPTS[@]}"

echo "perf.data 保存到 $PERF_DATA"
# 2) 符号化
$PERF script -i "${FPATH_PREFIX}.data" > "${FPATH_PREFIX}.perf"

# 3) 折叠栈 -> SVG
/opt/FlameGraph/stackcollapse-perf.pl "${FPATH_PREFIX}.perf" |
/opt/FlameGraph/flamegraph.pl          \
        --title "FIO ${MODE_TAG} ${BS} ${EXPERIMENT_TITLE}" \
        --countname "samples" \
        > "${FPATH_PREFIX}.svg"

echo "火焰图输出: ${FPATH_PREFIX}.svg"
# 7. 测试完成
echo "----------------------------------------------------------"
echo "测试完成！"
echo "结果已保存在: ${OUTPUT_FILE_PATH}"
echo "----------------------------------------------------------"
