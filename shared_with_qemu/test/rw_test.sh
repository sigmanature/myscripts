#!/bin/bash
# rw_test.sh - 一个用于精确读写文件块的 Shell 脚本

# 在命令失败时立即退出
set -e

# --- 默认值 ---
TARGET_FILE=""
KEEP_FILE=0 # 0 表示不保留，1 表示保留
BLOCK_SIZE_STR="" # 新增: 用于存储块大小参数

# --- 清理函数 ---
cleanup() {
  # 只有在 KEEP_FILE 为 0 且 TARGET_FILE 变量非空且确实是一个文件时才删除
  if [ "$KEEP_FILE" -eq 0 ] && [ -n "$TARGET_FILE" ] && [ -f "$TARGET_FILE" ]; then
    echo "==> 清理: 删除文件 '$TARGET_FILE'..."
    # 移除可能存在的不可变属性，以确保能删除
    # 使用 chattr 前最好检查文件是否存在
    [ -f "$TARGET_FILE" ] && chattr -i "$TARGET_FILE" >/dev/null 2>&1 || true
    rm -f "$TARGET_FILE"
  else
    echo "==> 清理: 根据选项或文件状态，无需删除。"
  fi
}

# --- 注册清理函数 ---
trap cleanup EXIT INT TERM

# --- 帮助信息 ---
usage() {
    echo "用法: $0 -f <文件路径> [选项] <操作> <偏移量> <大小>"
    echo ""
    echo "必需参数:"
    echo "  -f, --file <路径>   指定要操作的目标文件"
    echo ""
    echo "操作:"
    echo "  r, read             从文件读取数据"
    echo "  w, write            向文件写入数据 (来自 /dev/zero)"
    echo ""
    echo "选项:"
    echo "  -k, --keep          操作完成后保留文件，不删除"
    echo "  -b, --bs <大小>     指定 dd 命令的块大小 (例如 4k, 1m)。" # 新增
    echo "                      如果未指定，则块大小等于总大小 (一次IO)。" # 新增
    echo "  -h, --help          显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  # 从 my_file.dat 偏移量 1k 的地方，以 4k 为块大小，读取 1m 数据"
    echo "  $0 -f my_file.dat -k -b 4k r 1k 1m"
    echo ""
    echo "  # 向 my_file.dat 偏移量 0 的地方写入 4m 数据 (单次IO)"
    echo "  $0 -f my_file.dat w 0 4m"
    echo ""
    echo "支持单位: b (字节), k (千字节), m (兆字节), g (千兆字节)"
}

# --- 解析命令行参数 ---
while [ "$#" -gt 0 ]; do
  case "$1" in
    -f|--file)
      TARGET_FILE="$2"
      shift 2
      ;;
    -k|--keep)
      KEEP_FILE=1
      shift 1
      ;;
    # 新增: 解析 -b/--bs 参数
    -b|--bs)
      BLOCK_SIZE_STR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "错误: 未知的选项 '$1'" >&2
      usage
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

# --- 检查必需参数 ---
if [ -z "$TARGET_FILE" ]; then
    echo "错误: 必须使用 -f 或 --file 指定目标文件。" >&2
    usage
    exit 1
fi

# --- 检查剩余的位置参数 ---
if [ "$#" -ne 3 ]; then
    echo "错误: 需要提供 <操作>, <偏移量>, <大小> 这三个参数。" >&2
    usage
    exit 1
fi

OPERATION="$1"
OFFSET_STR="$2"
SIZE_STR="$3"

# --- 函数：解析带单位的字节数 ---
parse_bytes() {
    local input="${1,,}"
    input="${input%b}"
    local num="${input%[kmg]}"
    local unit="${input##*[0-9]}"
    local factor=1

    if ! [[ "$num" =~ ^[0-9]+$ ]]; then
        echo "错误: 无效的数字部分 '$num' 在 '$1' 中" >&2; exit 1
    fi

    case "$unit" in
        k) factor=1024 ;;
        m) factor=$((1024 * 1024)) ;;
        g) factor=$((1024 * 1024 * 1024)) ;;
        '') factor=1 ;;
        *) echo "错误: 无效的单位 '$unit' 在 '$1' 中" >&2; exit 1 ;;
    esac
    echo $((num * factor))
}

OFFSET_BYTES=$(parse_bytes "$OFFSET_STR")
SIZE_BYTES=$(parse_bytes "$SIZE_STR")

# --- 新增: 计算 dd 的 bs 和 count ---
DD_BS=""
DD_COUNT=""

if [ -n "$BLOCK_SIZE_STR" ]; then
    # 如果用户指定了 bs
    BLOCK_SIZE_BYTES=$(parse_bytes "$BLOCK_SIZE_STR")
    if [ "$BLOCK_SIZE_BYTES" -eq 0 ]; then
        echo "错误: 块大小(bs)不能为 0。" >&2; exit 1
    fi
    DD_BS=$BLOCK_SIZE_BYTES
    # 使用向上取整的除法，确保所有数据都被读/写
    DD_COUNT=$(((SIZE_BYTES + BLOCK_SIZE_BYTES - 1) / BLOCK_SIZE_BYTES))
else
    # 如果用户未指定 bs，则行为和以前一样
    DD_BS=$SIZE_BYTES
    DD_COUNT=1
fi

# --- 主逻辑 ---
case "$OPERATION" in
  r|read)
    echo "--- 读取操作 ---"
    echo "文件: $TARGET_FILE"
    echo "偏移: $OFFSET_BYTES 字节 ($OFFSET_STR)"
    echo "总大小: $SIZE_BYTES 字节 ($SIZE_STR)"
    echo "块大小(bs): $DD_BS 字节" # 修改
    echo "块数量(count): $DD_COUNT" # 修改
    echo "------------------"

    if [ ! -f "$TARGET_FILE" ]; then
        REQUIRED_SIZE=$((OFFSET_BYTES + SIZE_BYTES))
        echo "文件不存在，正在创建大小至少为 ${REQUIRED_SIZE} 字节的稀疏文件..."
        mkdir -p "$(dirname "$TARGET_FILE")"
        truncate -s "$REQUIRED_SIZE" "$TARGET_FILE"
        echo "文件创建成功。"
    fi

    echo "正在执行 dd 读取..."
    # 修改: 使用计算好的 $DD_BS 和 $DD_COUNT
    dd if="$TARGET_FILE" ibs=1 skip="$OFFSET_BYTES" bs="$DD_BS" count="$DD_COUNT" of=/dev/null status=progress
    echo "读取操作完成。"
    ;;

  w|write)
    echo "--- 写入操作 ---"
    echo "文件: $TARGET_FILE"
    echo "偏移: $OFFSET_BYTES 字节 ($OFFSET_STR)"
    echo "总大小: $SIZE_BYTES 字节 ($SIZE_STR)"
    echo "块大小(bs): $DD_BS 字节" # 修改
    echo "块数量(count): $DD_COUNT" # 修改
    echo "------------------"

    REQUIRED_SIZE=$((OFFSET_BYTES + SIZE_BYTES))
    mkdir -p "$(dirname "$TARGET_FILE")"

    if [ ! -f "$TARGET_FILE" ]; then
        REQUIRED_SIZE=$((OFFSET_BYTES + SIZE_BYTES))
        echo "文件不存在，正准备touch"
        touch "$TARGET_FILE"
        echo "文件创建成功。"
    fi

    echo "正在执行 dd 写入 (数据源: /dev/zero)..."
    # 修改: 使用计算好的 $DD_BS 和 $DD_COUNT
    dd if=/dev/zero of="$TARGET_FILE" obs=1 seek="$((OFFSET_BYTES / DD_BS))" bs="$DD_BS" count="$DD_COUNT" conv=notrunc status=progress
    echo "写入操作完成。"
    ;;

  *)
    echo "错误: 未知的操作 '$OPERATION'。请使用 'read' 或 'write'。" >&2
    usage
    exit 1
    ;;
esac

exit 0
