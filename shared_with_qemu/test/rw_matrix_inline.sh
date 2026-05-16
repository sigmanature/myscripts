#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

export KEY="${KEY:-/opt/test-secrets/fscrypt-ci.key}"
SCRIPT="${SCRIPT:-$SCRIPT_DIR/rw_test.py}"
MNT_F2FS="${MNT_F2FS:-/mnt/f2fs}"
export ENC_DIR="${ENC_DIR:-$MNT_F2FS/enc_test}"

PATTERN_MODE="${PATTERN_MODE:-filepos}"
PATTERN_TOKEN="${PATTERN_TOKEN:-PyWrtDta}"
SEED="${SEED:-0}"
CHUNK="${CHUNK:-1m}"
PATTERN_GEN="${PATTERN_GEN:-stream}"
READBACK="${READBACK:-stream}"
VERIFY_MODE="${VERIFY_MODE:-disk}"
LOOPS="${LOOPS:-1}"
READ_BEFORE_WRITE_PASSES="${READ_BEFORE_WRITE_PASSES:-1}"
INCLUDE_READ_THEN_WRITE="${INCLUDE_READ_THEN_WRITE:-1}"
DROP_AFTER_PREPARE="${DROP_AFTER_PREPARE:-1}"

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "[FATAL] missing cmd: $1"; exit 2; }; }
need_cmd python3
need_cmd fscrypt
need_cmd findmnt
need_cmd lsattr

if [[ ! -f "$SCRIPT" ]]; then
  echo "[FATAL] cannot find script: $SCRIPT"
  exit 2
fi

if [[ ! -d "$ENC_DIR" ]]; then
  echo "[FATAL] ENC_DIR not found: $ENC_DIR"
  exit 2
fi

ensure_inlinecrypt_mount() {
  local opts
  opts="$(findmnt -no OPTIONS --target "$MNT_F2FS" 2>/dev/null || true)"
  if [[ -z "$opts" ]]; then
    echo "[FATAL] mount point not found: $MNT_F2FS"
    exit 2
  fi
  if [[ ",$opts," != *",inlinecrypt,"* ]]; then
    echo "[FATAL] $MNT_F2FS is not mounted with inlinecrypt"
    echo "        current options: $opts"
    exit 2
  fi
  echo "[INFO] inlinecrypt enabled on $MNT_F2FS"
}

ensure_enc_dir_ready() {
  local st
  st="$(fscrypt status "$ENC_DIR" 2>/dev/null || true)"
  if [[ "$st" != *"encrypted with fscrypt"* ]]; then
    echo "[FATAL] ENC_DIR is not encrypted with fscrypt: $ENC_DIR"
    fscrypt status "$ENC_DIR" || true
    exit 2
  fi
  if ! lsattr -d "$ENC_DIR" 2>/dev/null | awk '{print $1}' | grep -q 'E'; then
    echo "[FATAL] ENC_DIR missing fscrypt 'E' attribute: $ENC_DIR"
    lsattr -d "$ENC_DIR" || true
    exit 2
  fi
}

fscrypt unlock "$ENC_DIR" --key="$KEY" >/dev/null 2>&1 || true
ensure_inlinecrypt_mount
ensure_enc_dir_ready

cmd=(
  python3 "$SCRIPT" matrix
  --target "inline=$ENC_DIR"
  --pattern-mode "$PATTERN_MODE"
  --token "$PATTERN_TOKEN"
  --seed "$SEED"
  --chunk "$CHUNK"
  --pattern-gen "$PATTERN_GEN"
  --readback "$READBACK"
  --verify-mode "$VERIFY_MODE"
  --loops "$LOOPS"
  --read-before-write-passes "$READ_BEFORE_WRITE_PASSES"
)

if [[ "$INCLUDE_READ_THEN_WRITE" == "0" ]]; then
  cmd+=(--no-read-then-write)
fi

if [[ "$DROP_AFTER_PREPARE" == "0" ]]; then
  cmd+=(--no-drop-after-prepare)
fi

echo "[INFO] SCRIPT=$SCRIPT"
echo "[INFO] ENC_DIR=$ENC_DIR"
echo "[INFO] VERIFY_MODE=$VERIFY_MODE LOOPS=$LOOPS"
echo "[INFO] PATTERN_MODE=$PATTERN_MODE TOKEN=$PATTERN_TOKEN CHUNK=$CHUNK"

"${cmd[@]}" "$@"
