#!/bin/bash

MODULE_NAME="myfirstmodule"
MODULE_PATH="./myfirstmodule.ko" # 假设模块ko文件和脚本在同一目录
LOG_FILE="memory_fragmentation_log.txt"
COLLECTION_INTERVAL=1 # 数据收集间隔 (秒)


# 模块参数 (可以根据需要调整)
NUM_ALLOCATIONS=3000
DELAY_MS=10
DURATION=$(( (NUM_ALLOCATIONS * DELAY_MS) / 1000 + 10 )) # 实验持续时间 (秒)
echo $DURATION"秒实验持续时间"
# 清空日志文件 (如果存在)
> "$LOG_FILE"

# 记录开始时间
echo "实验开始时间: $(date)" >> "$LOG_FILE"

# 加载内核模块并传递参数，放入后台运行
insmod "$MODULE_PATH" num_allocations="$NUM_ALLOCATIONS" delay_ms="$DELAY_MS" &
echo "模块加载命令已发送到后台，脚本继续执行..."
sleep 2  # 等待模块加载和初始化 (可选，但推荐)\
echo "模块加载后等待2秒结束，继续数据收集"

START_TIME=$(date +%s)
echo "$START_TIME 是开始时间"
END_TIME=$((START_TIME + DURATION))

while [ $(date +%s) -lt "$END_TIME" ]; do
    TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
    echo "-------------------- $TIMESTAMP --------------------" >> "$LOG_FILE"
    echo "--- /proc/buddyinfo ---" >> "$LOG_FILE"
    cat /proc/buddyinfo >> "$LOG_FILE"
    echo "--- /proc/meminfo ---" >> "$LOG_FILE"
    cat /proc/meminfo >> "$LOG_FILE"
    # echo "--- /proc/slabinfo ---" >> "$LOG_FILE"
    # cat /proc/slabinfo >> "$LOG_FILE"

    sleep "$COLLECTION_INTERVAL"
done

# 卸载内核模块
rmmod "$MODULE_NAME"

# 记录结束时间
echo "实验结束时间: $(date)" >> "$LOG_FILE"

echo "数据收集完成，日志文件: $LOG_FILE"
