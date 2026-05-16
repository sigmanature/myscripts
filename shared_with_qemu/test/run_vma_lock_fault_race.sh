#!/bin/bash
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN=/tmp/vma_lock_fault_race
WORK_FILE="${1:-/mnt/f2fs/vma_lock_fault_race.bin}"
OUTDIR="${2:-/mnt/f2fs/vma_lock_trace_$(date +%Y%m%d-%H%M%S)}"

mkdir -p "$OUTDIR"

gcc -pthread -O2 -Wall -Wextra -o "$BIN" "$SELF_DIR/vma_lock_fault_race.c"

"$SELF_DIR/trace_vma_lock_shard.sh" "$OUTDIR" &
TRACE_PID=$!

cleanup() {
  kill "$TRACE_PID" 2>/dev/null || true
  wait "$TRACE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$BIN" "$WORK_FILE" \
  --file-mb 512 \
  --readers 8 \
  --writer-delay-ms 20 \
  --writer-iters 500000 \
  --reader-rounds 1 \
  --drop-caches \
  --pin-cpus
