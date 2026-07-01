#!/system/bin/sh
set -u
set -o pipefail 2>/dev/null || true

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <outdir>" >&2
  exit 2
fi

OUTDIR=$1
TRACEFS=${TRACEFS:-/sys/kernel/tracing}
EVENT_DIR="$TRACEFS/events/mmap_lock"
READY_FILE="$OUTDIR/trace.ready"
HIT_FILE="$OUTDIR/had_reader.hit"
STATUS_FILE="$OUTDIR/trace.status"
TRACE_FILTER_TGID=${TRACE_FILTER_TGID:-}
EVENTS="vma_start_write_begin vma_start_write_wait vma_start_write_done vma_start_read vma_end_read vma_reader_released vma_start_read_fail fault_mmap_lock_fallback"

mkdir -p "$OUTDIR"
: >"$OUTDIR/writer.trace"
: >"$OUTDIR/reader.trace"
: >"$OUTDIR/fault.trace"
: >"$OUTDIR/mmap_readlock.trace"
: >"$OUTDIR/filemap.trace"
: >"$OUTDIR/other.trace"
: >"$STATUS_FILE"

fail() {
  echo "error: $*" | tee -a "$STATUS_FILE" >&2
  exit 1
}

write_tracefs() {
  value=$1
  path=$2

  printf '%s\n' "$value" >"$path" 2>/dev/null || return 1
}

[ -d "$EVENT_DIR" ] || fail "missing mmap_lock trace event dir: $EVENT_DIR"
for evt in $EVENTS; do
  [ -e "$EVENT_DIR/$evt/enable" ] || fail "missing trace event: mmap_lock/$evt"
done

write_tracefs 0 "$TRACEFS/tracing_on" || fail "cannot stop tracing"
write_tracefs nop "$TRACEFS/current_tracer" || true
write_tracefs 32768 "$TRACEFS/buffer_size_kb" || true
for evt in $EVENTS; do
  write_tracefs 0 "$EVENT_DIR/$evt/enable" || true
  if [ -n "$TRACE_FILTER_TGID" ]; then
    write_tracefs "tgid == $TRACE_FILTER_TGID" "$EVENT_DIR/$evt/filter" || fail "cannot filter mmap_lock/$evt by tgid=$TRACE_FILTER_TGID"
  else
    write_tracefs 0 "$EVENT_DIR/$evt/filter" || true
  fi
done
printf '\n' >"$TRACEFS/trace" 2>/dev/null || fail "cannot clear trace buffer"
for evt in $EVENTS; do
  write_tracefs 1 "$EVENT_DIR/$evt/enable" || fail "cannot enable mmap_lock/$evt"
done
write_tracefs 1 "$TRACEFS/tracing_on" || fail "cannot start tracing"
date '+trace_started=%Y-%m-%dT%H:%M:%S%z' >>"$STATUS_FILE"
printf 'trace_filter_tgid=%s\n' "${TRACE_FILTER_TGID:-none}" >>"$STATUS_FILE"
for evt in $EVENTS; do
  printf '%s\n' "$evt"
done >"$OUTDIR/enabled_events.txt"
: >"$READY_FILE"

cleanup() {
  write_tracefs 0 "$TRACEFS/tracing_on" || true
  for evt in $EVENTS; do
    write_tracefs 0 "$EVENT_DIR/$evt/enable" || true
  done
  date '+trace_stopped=%Y-%m-%dT%H:%M:%S%z' >>"$STATUS_FILE" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while IFS= read -r line; do
  case "$line" in
    *vma_start_write_*)
      printf '%s\n' "$line" >>"$OUTDIR/writer.trace"
      case "$line" in
        *vma_start_write_wait:*had_reader=true*)
          printf '%s\n' "$line" >"$HIT_FILE"
          exit 0
          ;;
      esac
      ;;
    *vma_start_read:*|*vma_end_read:*|*vma_reader_released:*)
      printf '%s\n' "$line" >>"$OUTDIR/reader.trace"
      ;;
    *vma_start_read_fail:*)
      printf '%s\n' "$line" >>"$OUTDIR/fault.trace"
      ;;
    *fault_mmap_lock_fallback:*)
      printf '%s\n' "$line" >>"$OUTDIR/fault.trace"
      printf '%s\n' "$line" >>"$OUTDIR/mmap_readlock.trace"
      ;;
    *filemap_*)
      printf '%s\n' "$line" >>"$OUTDIR/filemap.trace"
      ;;
    *)
      printf '%s\n' "$line" >>"$OUTDIR/other.trace"
      ;;
  esac
done <"$TRACEFS/trace_pipe"
