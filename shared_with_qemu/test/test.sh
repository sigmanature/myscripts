#!/bin/bash

# --- 步骤 1: 设置变量和参数 ---
FLAMEGRAPH_DIR="/opt/FlameGraph"
RESULTS_DIR="$HOME/perf_results"
mkdir -p "$RESULTS_DIR"

# --- 使用说明函数 ---
usage() {
    echo "用法: $0 <实验标题> [rw_test.sh 的参数...]"
    echo ""
    echo "此脚本会自动从 rw_test.sh 的参数中提取读写模式(r/w)和块大小(-b)并添加到输出文件名中。"
    echo ""
    echo "参数:"
    echo "  <实验标题>:         用于标识此次实验的标题 (例如 'f2fs_test')。"
    echo "                      这将用作输出文件名的基础部分。"
    echo "  [rw_test.sh 参数...]: 要传递给 rw_test.sh 脚本的所有参数。"
    echo "                      例如: -f /path/to/file -k -b 4k r 0 512m"
    echo ""
    echo "示例:"
    echo "  # 对 /mnt/f2fs/file.dat 进行 512m 读取测试，块大小为 4k"
    echo "  # 文件名将自动包含 'read_bs_4k'"
    echo "  $0 f2fs_read_test -f /mnt/f2fs/file.dat -k -b 4k r 0 512m"
    echo ""
    echo "  # 对 /tmp/test.img 进行 1g 写入测试，无指定块大小"
    echo "  # 文件名将自动包含 'write'"
    echo "  $0 tmpfs_write_test -f /tmp/test.img w 0 1g"
}

# --- 参数校验 ---
# 检查是否提供了实验标题
if [ -z "$1" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
    exit 0
fi

# 第一个参数是实验标题
EXPERIMENT_TITLE=$1
# 使用 shift 将第一个参数移出，剩下的 $@ 就是要传递给子脚本的参数
shift

# 检查是否为子脚本提供了参数
if [ "$#" -eq 0 ]; then
    echo "错误: 请提供要传递给 rw_test.sh 的参数。" >&2
    usage
    exit 1
fi

# --- 【新增功能】: 解析参数以生成动态文件名后缀 ---
echo "==> 步骤 1.5: 解析参数以生成文件名..."

RW_MODE=""
BLOCK_SIZE=""
# 使用一个标志位来帮助我们找到 -b 后面的值
NEXT_IS_BS=false

# 遍历所有要传递给 rw_test.sh 的参数 ("$@")
for arg in "$@"; do
    # 如果上一个参数是 -b，那么当前参数就是块大小
    if [[ "$NEXT_IS_BS" == true ]]; then
        BLOCK_SIZE="$arg"
        NEXT_IS_BS=false
        continue # 继续下一个循环，避免当前参数被误判为 r/w
    fi

    case "$arg" in
        -b)
            # 找到 -b 标志，设置标志位，下一个参数将是块大小
            NEXT_IS_BS=true
            ;;
        r)
            # 找到读模式
            RW_MODE="read"
            ;;
        w)
            # 找到写模式
            RW_MODE="write"
            ;;
    esac
done

# 构建文件名后缀
FILENAME_SUFFIX=""
if [ -n "$RW_MODE" ]; then
    FILENAME_SUFFIX+="_${RW_MODE}"
fi
if [ -n "$BLOCK_SIZE" ]; then
    # 添加 'bs_' 前缀使文件名更清晰
    FILENAME_SUFFIX+="_bs_${BLOCK_SIZE}"
fi

echo "检测到模式: ${RW_MODE:-未指定}, 块大小: ${BLOCK_SIZE:-未指定}"
echo "----------------------------------------"
# --- 【新增功能结束】 ---


# --- 步骤 2: 运行 perf 并生成火焰图 ---
echo "==> 步骤 2: 运行 perf record..."
echo "实验标题: ${EXPERIMENT_TITLE}"
echo "传递给 rw_test.sh 的参数: $@"
echo "----------------------------------------"

TIMESTAMP=$(date +'%Y%m%d-%H%M%S')

# --- 【核心修改点】: 在文件名中加入动态后缀 ---
# 文件名现在由 标题 + 动态后缀 + 时间戳 组成
SVG_FILE="${RESULTS_DIR}/${EXPERIMENT_TITLE}${FILENAME_SUFFIX}_${TIMESTAMP}.svg"
PERF_DATA="${RESULTS_DIR}/${EXPERIMENT_TITLE}${FILENAME_SUFFIX}_${TIMESTAMP}.data"

echo "将生成以下文件:"
echo "  性能数据: ${PERF_DATA}"
echo "  SVG 火焰图: ${SVG_FILE}"
echo "----------------------------------------"

# 使用 "$@" 将所有剩余参数安全地传递给 rw_test.sh
# 确保 rw_test.sh 在当前目录或 $PATH 中，且有执行权限 (chmod +x rw_test.sh)
perf record -o "$PERF_DATA" -F 99 --call-graph dwarf -- \
  ./rw_test.sh "$@"

# 检查 perf record 是否成功
if [ $? -ne 0 ]; then
    echo "perf record 失败，请检查权限或 perf 工具是否安装正确。"
    exit 1
fi
echo "性能数据已保存到: ${PERF_DATA}"
echo ""

# --- 步骤 3: 生成文本报告 ---
echo "==> 步骤 3: 生成文本报告..."
perf report -i "$PERF_DATA" > "${PERF_DATA}_report.txt"
echo "文本报告已保存到: ${PERF_DATA}_report.txt"
echo ""

# --- 步骤 4: 生成火焰图 ---
echo "==> 步骤 4: 生成火焰图..."
if [ ! -f "${FLAMEGRAPH_DIR}/stackcollapse-perf.pl" ]; then
    echo "错误: 在 ${FLAMEGRAPH_DIR} 中找不到 stackcollapse-perf.pl"
    exit 1
fi

# 注意: 如果 perf record 是以普通用户运行的，这里可能不需要 sudo
# 但如果 perf record 需要 sudo，那么 perf script 也需要
perf script -i "$PERF_DATA"            \
  | ${FLAMEGRAPH_DIR}/stackcollapse-perf.pl \
  > /tmp/all.folded

# (这部分逻辑保持不变)
# 如果需要，你可以根据实验标题来决定是否执行这部分 grep
grep 'page_cache_ra_order' /tmp/all.folded > /tmp/ra.folded
if [ -s /tmp/ra.folded ]; then
    sed -E 's/^.*;page_cache_ra_order/page_cache_ra_order/' /tmp/ra.folded > /tmp/ra_new.folded
    ${FLAMEGRAPH_DIR}/flamegraph.pl \
        --minwidth 8                  \
        /tmp/ra_new.folded > "$SVG_FILE"
else
    echo "警告: 未在性能数据中找到 'page_cache_ra_order'，将为完整数据生成火焰图。"
    ${FLAMEGRAPH_DIR}/flamegraph.pl \
        --minwidth 8                  \
        /tmp/all.folded > "$SVG_FILE"
fi

# 检查火焰图是否生成成功
if [ -s "$SVG_FILE" ]; then
    echo "火焰图生成成功!"
    echo "SVG 文件位于: ${SVG_FILE}"
    echo "请在浏览器中打开此文件进行分析。"
else
    echo "错误: 火焰图文件生成失败或为空。"
    echo "请检查 perf script 和 FlameGraph 脚本的输出。"
fi
