CACHE=/sys/kernel/slab/f2fs_ffs_slab
OUT=/tmp/f2fs_ffs_slab_$(date +%F_%H%M%S).csv

# 你关心的“状态类”字段（一直有）
FIELDS="objects total_objects slabs cpu_slabs cpu_partial partial objects_partial order objs_per_slab object_size slab_size"

# 如果内核开了 CONFIG_SLUB_STATS，会额外出现很多“计数器类”字段（可清零、可算频率）
# ABI 文档里列了 alloc_slowpath 等统计项，并明确是 CONFIG_SLUB_STATS 才有 ([MJM Wired](https://mjmwired.net/kernel/Documentation/ABI/testing/sysfs-kernel-slab?utm_source=chatgpt.com))
STAT_FIELDS=""
fields=(
  alloc_slab
  alloc_fastpath
  alloc_slowpath
  alloc_from_partial
  order_fallback
  sheaf_return_fast
  sheaf_return_slow
  sheaf_prefill_fast
  sheaf_prefill_slow
  sheaf_prefill_oversize
)

for f in "${fields[@]}"; do
  [ -f "$CACHE/$f" ] && STAT_FIELDS="$STAT_FIELDS $f"
done

echo "ts,$(echo $FIELDS $STAT_FIELDS | tr ' ' ',')" | tee "$OUT"

while :; do
  ts=$(date +%s.%N)
  line="$ts"
  for f in $FIELDS $STAT_FIELDS; do
    v=$(cat "$CACHE/$f" 2>/dev/null | tr -d '\n')
    line="$line,$v"
  done
  echo "$line" | tee -a "$OUT"
  sleep 1
done
