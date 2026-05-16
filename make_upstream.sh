#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BASE="${BASE:-$BASE_DEFAULT}"
ARCH="${ARCH:-arm64}"
CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}"
JOBS="${JOBS:-$(nproc)}"

SRC="${BASE}/f2fs"
OUT="${BASE}/f2fs_upstream"
CONFIG_SEED=""
LOGFILE=""
REFRESH_CONFIG=0

usage() {
  cat <<EOF
usage: $(basename "$0") [options] [make-target ...]

Build a kernel tree with a separate O= output directory.

Defaults preserve the old upstream behavior:
  src     : ${BASE}/f2fs
  out     : ${BASE}/f2fs_upstream
  targets : olddefconfig Image

options:
  --src DIR            kernel source tree or worktree
  --out DIR            O= output directory
  --config-seed PATH   seed config file; accepts plain .config or config_data.gz
  --log FILE           build log path (default: OUT/makelog.txt)
  -j, --jobs N         parallel jobs (default: nproc)
  --refresh-config     overwrite OUT/.config from the selected seed
  -h, --help           show this help

examples:
  $(basename "$0")
  $(basename "$0") --src "${BASE}/f2fs_gc_path_logs" --out "${BASE}/f2fs_gc_path_logs_out" --config-seed "${BASE}/f2fs_upstream/.config.old"
  $(basename "$0") --src "${BASE}/f2fs_gc_path_logs" --out "${BASE}/f2fs_gc_path_logs_out" --refresh-config Image
EOF
}

MAKE_TARGETS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)
      SRC="$2"
      shift 2
      ;;
    --out)
      OUT="$2"
      shift 2
      ;;
    --config-seed)
      CONFIG_SEED="$2"
      shift 2
      ;;
    --log)
      LOGFILE="$2"
      shift 2
      ;;
    -j|--jobs)
      JOBS="$2"
      shift 2
      ;;
    --refresh-config)
      REFRESH_CONFIG=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      MAKE_TARGETS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#MAKE_TARGETS[@]} -eq 0 ]]; then
  MAKE_TARGETS=(Image)
fi

if [[ -z "$LOGFILE" ]]; then
  LOGFILE="${OUT}/makelog.txt"
fi

resolve_default_seed() {
  local candidate

  for candidate in \
    "${BASE}/f2fs_upstream/.config.old" \
    "${BASE}/f2fs_upstream/.config" \
    "${BASE}/f2fs_upstream/kernel/config_data.gz"
  do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

seed_config_if_needed() {
  local seed="$1"

  mkdir -p "$OUT"
  if [[ -f "${OUT}/.config" && "$REFRESH_CONFIG" -eq 0 ]]; then
    return 0
  fi

  if [[ -z "$seed" ]]; then
    echo "[WARN] no config seed selected; keeping existing OUT/.config if present" >&2
    return 0
  fi

  case "$seed" in
    *.gz)
      gzip -dc "$seed" > "${OUT}/.config"
      ;;
    *)
      cp "$seed" "${OUT}/.config"
      ;;
  esac
}

if [[ ! -d "$SRC" ]]; then
  echo "[FATAL] source tree not found: $SRC" >&2
  exit 2
fi

if [[ -z "$CONFIG_SEED" ]]; then
  CONFIG_SEED="$(resolve_default_seed || true)"
fi

seed_config_if_needed "$CONFIG_SEED"

mkdir -p "$OUT"

echo "[BUILD] src=$SRC"
echo "[BUILD] out=$OUT"
echo "[BUILD] arch=$ARCH cross=${CROSS_COMPILE}"
echo "[BUILD] jobs=$JOBS"
if [[ -n "$CONFIG_SEED" ]]; then
  echo "[BUILD] config_seed=$CONFIG_SEED"
else
  echo "[BUILD] config_seed=<none>"
fi
echo "[BUILD] logfile=$LOGFILE"
echo "[BUILD] targets=olddefconfig ${MAKE_TARGETS[*]}"

make \
  -C "$SRC" \
  O="$OUT" \
  ARCH="$ARCH" \
  CROSS_COMPILE="$CROSS_COMPILE" \
  olddefconfig \
  "${MAKE_TARGETS[@]}" \
  -j"$JOBS" \
  &> "$LOGFILE"
