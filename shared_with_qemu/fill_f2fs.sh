#!/bin/bash

mount_point="/mnt/f2fs"  # 你的 F2FS 挂载点
file_prefix="data_file_"
file_count=0
block_size_kb=4        # 假设块大小为 4KB
blocks_per_file=8      # 每个文件至少占用 8 个块
data_size_kb=$((block_size_kb * blocks_per_file)) # 每个文件的数据大小 (KB)
data_size_bytes=$((data_size_kb * 1024)) # 每个文件的数据大小 (Bytes)

while true; do
    filename="${mount_point}/${file_prefix}${file_count}"
    # 使用 dd 命令写入数据
    dd if=/dev/urandom of="${filename}" bs=1024 count="${data_size_kb}" status=none
    if [ $? -ne 0 ]; then
        echo "文件系统空间已满，创建文件失败！"
        break
    fi
    file_count=$((file_count + 1))
    echo "创建文件: ${filename} (${data_size_kb}KB)"
done

echo "总共创建了 ${file_count} 个数据文件，每个文件大小约为 ${data_size_kb}KB。"
