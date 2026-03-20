#!/bin/bash
echo "Syncing filesystem to ensure all pending data is written..."
sync
echo "Dropping PageCache, dentries and inodes..."
# 必须用 sudo 或 root 权限运行
echo 3 | sudo tee /proc/sys/vm/drop_caches
echo "Caches dropped. System is ready for a cold read test."
