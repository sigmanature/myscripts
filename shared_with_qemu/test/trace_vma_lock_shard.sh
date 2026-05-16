#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <outdir>" >&2
  exit 2
fi

OUTDIR=$1
TRACEFS=${TRACEFS:-/sys/kernel/tracing}

mkdir -p "$OUTDIR"
: >"$OUTDIR/writer.trace"
: >"$OUTDIR/reader.trace"
: >"$OUTDIR/fault.trace"
: >"$OUTDIR/filemap.trace"
: >"$OUTDIR/other.trace"

echo 0 >"$TRACEFS/tracing_on"
echo nop >"$TRACEFS/current_tracer"
echo >"$TRACEFS/trace"
echo 1 >"$TRACEFS/events/mmap_lock/vma_lock_event/enable"
echo 1 >"$TRACEFS/tracing_on"

cleanup() {
  echo 0 >"$TRACEFS/tracing_on" || true
  echo 0 >"$TRACEFS/events/mmap_lock/vma_lock_event/enable" || true
}
trap cleanup EXIT INT TERM

stdbuf -oL cat "$TRACEFS/trace_pipe" | \
awk -v out="$OUTDIR" '
  /site=writer_/  { print > (out "/writer.trace");  next }
  /site=reader_/  { print > (out "/reader.trace");  next }
  /site=fault_/   { print > (out "/fault.trace");   next }
  /site=filemap_/ { print > (out "/filemap.trace"); next }
                   { print > (out "/other.trace") }
'
