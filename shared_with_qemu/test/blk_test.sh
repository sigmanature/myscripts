#!/usr/bin/env bash
# run_trace.sh  ―― 一键 blktrace + blkparse
# 用法: sudo ./run_trace.sh [mpage|iomap] [duration]
set -euo pipefail

##### 1. 参数 #####
dev="/dev/vdc"                     # 你的测试盘
mode="${1:-mpage}"                 # mpage 或 iomap
duration="${2:-18}"                # blktrace 录制时长（秒）
depth=128                          # -n 队列深度
mp="/mnt/f2fs"                     # F2FS 挂载点
file="$mp/lf.c"                    # 读测试用文件
blk_sz="4k"                        # block 大小
size="1G"                          # 读量
prefix="$mode"                     # blktrace/blktrace.* 前缀

case "$mode" in
  mpage) bench="bench_read_4k" ;;
  iomap) bench="iomap_read_4k" ;;
  *) echo "参数只能是 mpage 或 iomap" >&2; exit 1 ;;
esac

##### 2. 清理历史结果 #####
sudo rm -f "${prefix}".blktrace.* "${prefix}".blkparse.txt

##### 3. 启动 blktrace（自动计时退出）#####
echo ">>> blktrace: 设备=$dev 时长=${duration}s 深度=$depth"
sudo blktrace -d "$dev" -o "$prefix" -n "$depth" -w "$duration" &
trace_pid=$!

sleep 1  # 给 blktrace 1 s 初始化

##### 4. 跑 I/O workload #####
echo ">>> workload: bash rw_test.sh -k -f $file -b $blk_sz r 0 $size"
bash rw_test.sh -k -f "$file" -b "$blk_sz" r 0 "$size"

##### 5. 等待 blktrace 自动结束 #####
wait $trace_pid
echo ">>> blktrace 结束，开始解析"

##### 6. 解析 & 汇总 #####
if ls "${prefix}".blktrace.* &>/dev/null; then
    sudo blkparse -i "${prefix}".blktrace.* > "${prefix}".blkparse.txt
    echo "blkparse 结果 -> ${prefix}.blkparse.txt"

    # 如果还想做高级统计/可视化，加上 btt，例如：
    # sudo btt -i "${prefix}".blktrace.* -o "${prefix}".btt.txt
    # echo "btt 统计 -> ${prefix}.btt.txt"
else
    echo "没有生成 blktrace 文件，trace 可能失败" >&2
    exit 1
fi

echo ">>> 全流程完成 ✅"