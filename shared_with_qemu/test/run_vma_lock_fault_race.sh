#!/system/bin/sh
set -u
set -o pipefail 2>/dev/null || true

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${VMA_LOCK_RACE_BIN:-/tmp/vma_lock_fault_race}"
WORK_FILE="${1:-/mnt/f2fs/vma_lock_fault_race.bin}"
OUTDIR="${2:-/mnt/f2fs/vma_lock_trace_$(date +%Y%m%d-%H%M%S)}"
TRACEFS=${TRACEFS:-/sys/kernel/tracing}
LOCKFILE="${VMA_LOCK_RACE_LOCK:-/tmp/vma_lock_fault_race.lock}"
LOCKDIR="$LOCKFILE.d"
STATUS_FILE="$OUTDIR/run.status"
TRACE_PID=
WORKLOAD_PID=
WAIT_FILE="$OUTDIR/workload.go"

mkdir -p "$OUTDIR"
: >"$STATUS_FILE"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$STATUS_FILE" >&2
}

fail() {
  log "error: $*"
  exit 1
}

cleanup() {
  reason=${1:-exit}

  log "cleanup reason=$reason"
  if [ -n "${WORKLOAD_PID:-}" ] && kill -0 "$WORKLOAD_PID" 2>/dev/null; then
    kill -TERM "-$WORKLOAD_PID" 2>/dev/null || kill -TERM "$WORKLOAD_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "-$WORKLOAD_PID" 2>/dev/null || kill -KILL "$WORKLOAD_PID" 2>/dev/null || true
    wait "$WORKLOAD_PID" 2>/dev/null || true
  fi
  if [ -n "${TRACE_PID:-}" ] && kill -0 "$TRACE_PID" 2>/dev/null; then
    kill -TERM "$TRACE_PID" 2>/dev/null || true
    wait "$TRACE_PID" 2>/dev/null || true
  fi
  if [ -d "$LOCKDIR" ] && [ "$(cat "$LOCKDIR/pid" 2>/dev/null)" = "$$" ]; then
    rm -rf "$LOCKDIR"
  fi
}

trap 'cleanup INT; exit 130' INT
trap 'cleanup TERM; exit 143' TERM
trap 'cleanup EXIT' EXIT

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  OLD_PID=$(cat "$LOCKDIR/pid" 2>/dev/null || true)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    fail "another vma lock fault race run is active: pid=$OLD_PID lock=$LOCKDIR"
  fi
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || fail "cannot create lock dir $LOCKDIR"
fi
printf '%s\n' "$$" >"$LOCKDIR/pid" || fail "cannot record lock owner"

if [ ! -x "$BIN" ]; then
  CC=${CC:-gcc}
  log "building workload with CC=$CC BIN=$BIN"
  "$CC" -pthread -O2 -Wall -Wextra -o "$BIN" "$SELF_DIR/vma_lock_fault_race.c" \
    >>"$STATUS_FILE" 2>&1 || fail "failed to build workload"
fi

SYSCTL_PATH=
for p in /proc/sys/vm/filemap_fault_no_retry /proc/vm/filemap_fault_no_retry; do
  if [ -e "$p" ]; then
    SYSCTL_PATH=$p
    break
  fi
done
[ -n "$SYSCTL_PATH" ] || fail "missing filemap_fault_no_retry sysctl"
printf '1\n' >"$SYSCTL_PATH" 2>/dev/null || fail "cannot set $SYSCTL_PATH"
SYSCTL_VALUE=$(cat "$SYSCTL_PATH" 2>/dev/null)
[ "$SYSCTL_VALUE" = "1" ] || fail "$SYSCTL_PATH is $SYSCTL_VALUE, expected 1"
log "$SYSCTL_PATH=$SYSCTL_VALUE"

check_format_field() {
  evt=$1
  field=$2

  grep -Eq "field:.*[ *]$field(;|\\[)" "$TRACEFS/events/mmap_lock/$evt/format" 2>/dev/null
}

for evt in vma_start_write_begin vma_start_write_wait vma_start_write_done \
           vma_start_read vma_end_read vma_start_read_fail \
           fault_mmap_lock_fallback; do
  for field in tgid mm vm_start vm_end comm owner_comm; do
    check_format_field "$evt" "$field" || fail "mmap_lock/$evt format missing $field"
  done
done
check_format_field vma_start_write_wait had_reader || \
  fail "mmap_lock/vma_start_write_wait format missing had_reader"
log "tracepoint format check passed"

rm -f "$WAIT_FILE"
setsid "$BIN" "$WORK_FILE" \
  --file-mb 512 \
  --readers 8 \
  --writer-delay-ms 20 \
  --writer-iters 500000 \
  --reader-rounds 1 \
  --wait-file "$WAIT_FILE" \
  --drop-caches \
  --pin-cpus \
  >"$OUTDIR/workload.stdout" \
  2>"$OUTDIR/workload.stderr" &
WORKLOAD_PID=$!
log "workload_pid=$WORKLOAD_PID"

TRACE_FILTER_TGID="$WORKLOAD_PID" TRACEFS="$TRACEFS" "$SELF_DIR/trace_vma_lock_shard.sh" "$OUTDIR" &
TRACE_PID=$!
log "trace_pid=$TRACE_PID trace_filter_tgid=$WORKLOAD_PID"

ready_i=0
while [ "$ready_i" -lt 50 ]; do
  [ -e "$OUTDIR/trace.ready" ] && break
  if ! kill -0 "$TRACE_PID" 2>/dev/null; then
    fail "trace helper exited before ready"
  fi
  ready_i=$((ready_i + 1))
  sleep 0.1
done
[ -e "$OUTDIR/trace.ready" ] || fail "trace helper did not become ready"
log "trace ready"
: >"$WAIT_FILE"
log "workload released"

while kill -0 "$WORKLOAD_PID" 2>/dev/null; do
  if [ -s "$OUTDIR/had_reader.hit" ]; then
    log "had_reader=true detected; stopping workload"
    cleanup had_reader_hit
    exit 0
  fi
  sleep 0.2
done
wait "$WORKLOAD_PID"
WORKLOAD_RC=$?
WORKLOAD_PID=
log "workload_rc=$WORKLOAD_RC"

if [ -n "${TRACE_PID:-}" ] && kill -0 "$TRACE_PID" 2>/dev/null; then
  kill -TERM "$TRACE_PID" 2>/dev/null || true
  wait "$TRACE_PID" 2>/dev/null || true
fi
TRACE_PID=

if [ -s "$OUTDIR/had_reader.hit" ]; then
  log "result=had_reader_true"
else
  log "result=no_had_reader_true"
fi

exit "$WORKLOAD_RC"
