# 1. 设置 vm.dirty_ratio 为 100
echo 100 > /proc/sys/vm/dirty_ratio

# 2. 设置 vm.dirty_background_ratio 为 100
echo 100 > /proc/sys/vm/dirty_background_ratio

# 3. 设置 vm.dirty_writeback_centisecs 为 0
echo 0 > /proc/sys/vm/dirty_writeback_centisecs

echo 8640000 > /proc/sys/vm/dirty_expire_centisecs