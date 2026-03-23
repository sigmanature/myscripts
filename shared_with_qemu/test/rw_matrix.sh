#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export KEY=/opt/test-secrets/fscrypt-ci.key


# ----------------------------
# 自举 sudo：需要 drop_caches
# ----------------------------
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

# -------- 配置区：按需改 --------
SCRIPT="${SCRIPT:-$SCRIPT_DIR/rw_test.py}"      # 你的 Python 脚本路径
PLAIN_DIR="${PLAIN_DIR:-/mnt/f2fs}"                   # 普通文件目录（建议改到 /mnt/f2fs/...）
export ENC_DIR="${ENC_DIR:-/mnt/f2fs/enc_test}"         # 已启用 fscrypt 的加密目录
fscrypt unlock $ENC_DIR --key="$KEY"  >/dev/null 2>&1 || true
# 统一块大小语义：4K
BLOCK=4096

# 所有 baseline 文件逻辑大小必须 > 8K（你要求）
# aligned baseline（4K 对齐）
BASE_A="64k"        # 262144 bytes
HOLE_A="64k"
# unaligned baseline（用于“追加写：offset 不对齐”场景）
BASE_U="65537"      # 256k + 1
HOLE_U="65537"

# overwrite（覆盖区间写）4 组：offset/size 对齐组合
# 1) offset=0, size aligned
OW1_OFF="0"     OW1_SZ="64k"
# 2) offset=0, size unaligned
OW2_OFF="0"     OW2_SZ="65535"
# 3) offset!=0 aligned, size aligned
OW3_OFF="8k"    OW3_SZ="56k"
# 4) offset!=0 unaligned, size unaligned
OW4_OFF="4097"  OW4_SZ=$((65536 - 4097))

OVERWRITE_CASES=(
  "o0_aligned|$BASE_A|$OW1_OFF|$OW1_SZ"
  "o0_unaligned|$BASE_A|$OW2_OFF|$OW2_SZ"
  "onz_aligned|$BASE_A|$OW3_OFF|$OW3_SZ"
  "onz_unaligned|$BASE_A|$OW4_OFF|$OW4_SZ"
)

# append（追加写）4 组：通过 baseline size aligned/unaligned + size aligned/unaligned 组合得到
# 注意：append 语义 = 写入 offset = 当前文件 size（也就是“真正追加”）
APPEND_CASES=(
  "baseAligned_szAligned|$BASE_A|$OW1_SZ"
  "baseAligned_szUnaligned|$BASE_A|$OW2_SZ"
  "baseUnaligned_szAligned|$BASE_U|$OW1_SZ"
  "baseUnaligned_szUnaligned|$BASE_U|$OW2_SZ"
)

# 统一写入 pattern（覆盖/追加写都用它）
PATTERN_MODE="filepos"
PATTERN_TOKEN="PyWrtDta"
SEED="0"
CHUNK="1m"

# ----------------------------
# 基础工具检查
# ----------------------------
need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "[FATAL] missing cmd: $1"; exit 2; }; }
need_cmd python3
need_cmd truncate

if [[ ! -f "$SCRIPT" ]]; then
  echo "[FATAL] cannot find script: $SCRIPT"
  exit 2
fi
if [[ ! -d "$PLAIN_DIR" ]]; then
  mkdir -p "$PLAIN_DIR"
fi
if [[ ! -d "$ENC_DIR" ]]; then
  echo "[FATAL] ENC_DIR not found: $ENC_DIR"
  echo "        Please create it and make sure it is an fscrypt-encrypted directory on f2fs."
  exit 2
fi

echo "[INFO] SCRIPT    = $SCRIPT"
echo "[INFO] PLAIN_DIR = $PLAIN_DIR"
echo "[INFO] ENC_DIR   = $ENC_DIR"
echo "[INFO] BASE_A=$BASE_A BASE_U=$BASE_U HOLE_A=$HOLE_A HOLE_U=$HOLE_U"
echo "[INFO] PATTERN_MODE=$PATTERN_MODE TOKEN=$PATTERN_TOKEN CHUNK=$CHUNK"

# ----------------------------
# 辅助：drop_caches 冷读环境
# ----------------------------
drop_caches_3() {
  sync
  echo 3 > /proc/sys/vm/drop_caches
}

# ----------------------------
# 预设内容：把文件 [0, size) 填满 'A'
#   这一步用你的脚本 w + token=A 来做（可复现，且在加密目录同样适用）
# ----------------------------
fill_A() {
  local file="$1"
  local size="$2"
  python3 "$SCRIPT" w 0 "$size" -f "$file" \
    --fsync \
    --pattern-mode repeat \
    --token A \
    --pattern-gen stream \
    --chunk "$CHUNK" >/dev/null
}

# ----------------------------
# 覆盖/追加写：写入预设 pattern（PyWrtDta + filepos）
# ----------------------------
write_pattern() {
  local file="$1"
  local off="$2"
  local sz="$3"
  python3 "$SCRIPT" w "$off" "$sz" -f "$file" \
    --fsync \
    --verify-mode cache \
    --pattern-mode "$PATTERN_MODE" \
    --token "$PATTERN_TOKEN" \
    --seed "$SEED" \
    --pattern-gen stream \
    --chunk "$CHUNK" >/dev/null
}

# ----------------------------
# 整文件全校验（冷读）：baseline(A或0) + overlay(pattern)
#   - baseline_len：baseline 的逻辑大小（existing/hole 的 size）
#   - overlay_off/overlay_len：本次写入区间
#   - expected_len：写后文件逻辑大小
# ----------------------------
full_verify_disk() {
  local file="$1"
  local baseline_kind="$2"     # "A" or "ZERO"
  local baseline_len="$3"
  local overlay_off="$4"
  local overlay_len="$5"
  local expected_len="$6"

  drop_caches_3

  python3 - "$file" "$baseline_kind" "$baseline_len" "$overlay_off" "$overlay_len" "$expected_len" \
            "$PATTERN_MODE" "$PATTERN_TOKEN" "$SEED" "$CHUNK" <<'PY'
import os, sys

def parse_bytes(s: str) -> int:
    s = str(s).strip().lower()
    if not s:
        raise ValueError("empty")
    unit = s[-1]
    if unit.isalpha():
        num = s[:-1]
        factor = {'b':1,'k':1024,'m':1024**2,'g':1024**3}.get(unit)
        if factor is None:
            raise ValueError(s)
    else:
        num = s
        factor = 1
    if not num.isdigit():
        raise ValueError(s)
    return int(num) * factor

def repeat_bytes(token: bytes, start_idx: int, n: int) -> bytes:
    if n <= 0:
        return b""
    L = len(token)
    start_idx %= L
    out = bytearray(n)
    first = min(n, L - start_idx)
    out[:first] = token[start_idx:start_idx + first]
    filled = first
    while filled < n:
        take = min(L, n - filled)
        out[filled:filled + take] = token[:take]
        filled += take
    return bytes(out)

def gen_pattern(abs_pos: int, n: int, mode: str, token: bytes, seed: int, overlay_off: int) -> bytes:
    if mode == "counter":
        base = seed + abs_pos
        return bytes(((base + i) & 0xFF) for i in range(n))
    if mode == "filepos":
        return repeat_bytes(token, abs_pos, n)
    if mode == "repeat":
        # region-local repeat: abs_pos relative to overlay_off
        return repeat_bytes(token, abs_pos - overlay_off, n)
    raise ValueError(mode)

def hexdump(b: bytes, base: int = 0) -> str:
    out = []
    for i in range(0, len(b), 16):
        c = b[i:i+16]
        hx = " ".join(f"{x:02x}" for x in c).ljust(16*3-1)
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in c)
        out.append(f"{base+i:08x}  {hx}  |{asc}|")
    return "\n".join(out)

file = sys.argv[1]
baseline_kind = sys.argv[2]
baseline_len = int(sys.argv[3])
overlay_off = int(sys.argv[4])
overlay_len = int(sys.argv[5])
expected_len = int(sys.argv[6])
mode = sys.argv[7]
token = sys.argv[8].encode("utf-8")
seed = int(sys.argv[9])
chunk = parse_bytes(sys.argv[10])

st = os.stat(file)
if st.st_size != expected_len:
    print(f"[FAIL] size mismatch: actual={st.st_size} expected={expected_len}")
    sys.exit(1)

base_byte = 0x41 if baseline_kind == "A" else 0x00
overlay_end = overlay_off + overlay_len

fd = os.open(file, os.O_RDONLY)
try:
    pos = 0
    while pos < expected_len:
        n = min(chunk, expected_len - pos)
        got = os.pread(fd, n, pos)
        if len(got) != n:
            print(f"[FAIL] short read at {pos}: got {len(got)} expect {n}")
            sys.exit(1)

        # build expected chunk
        exp = bytearray([base_byte]) * n

        # baseline only valid for [0, baseline_len); beyond baseline_len baseline is "don't care"
        # 但我们的场景里 overlay 要么在 baseline 内（overwrite），要么从 baseline 末尾开始（append）。
        # 对 append：baseline_len 到 expected_len 这段本来不存在，expected 由 overlay 负责。

        # For append cases, baseline for [baseline_len, expected_len) should be 0 unless overwritten by overlay.
        if pos < baseline_len:
            pass
        else:
            # beyond baseline_len default should be 0
            exp[:] = b"\x00" * n

        # apply overlay if overlaps
        s = max(pos, overlay_off)
        e = min(pos + n, overlay_end)
        if s < e:
            rel = s - pos
            ln = e - s
            exp[rel:rel+ln] = gen_pattern(s, ln, mode, token, seed, overlay_off)

        if got != exp:
            # find first mismatch
            for i in range(n):
                if got[i] != exp[i]:
                    off = pos + i
                    lo = max(0, off - 64)
                    hi = min(expected_len, off + 64)
                    got2 = os.pread(fd, hi - lo, lo)
                    # rebuild expected slice [lo,hi)
                    exp2 = bytearray()
                    p = lo
                    while p < hi:
                        nn = min(hi - p, 1024)
                        # baseline
                        if p < baseline_len:
                            bb = 0x41 if baseline_kind == "A" else 0x00
                            tmp = bytearray([bb]) * nn
                        else:
                            tmp = bytearray(b"\x00") * nn
                        # overlay
                        ss = max(p, overlay_off)
                        ee = min(p + nn, overlay_end)
                        if ss < ee:
                            rr = ss - p
                            ll = ee - ss
                            tmp[rr:rr+ll] = gen_pattern(ss, ll, mode, token, seed, overlay_off)
                        exp2.extend(tmp)
                        p += nn

                    print(f"[FAIL] mismatch at file_off={off} (pos={pos} +{i})")
                    print("Expected slice:")
                    print(hexdump(bytes(exp2), base=lo))
                    print("Actual slice:")
                    print(hexdump(got2, base=lo))
                    sys.exit(1)

        pos += n

    print("[OK] full-file verify pass (disk cold read)")
finally:
    os.close(fd)
PY
}

# ----------------------------
# 跑单个测试：prepare -> write -> full verify
# ----------------------------
run_one() {
  local tag="$1"
  local file="$2"
  local baseline_kind="$3"     # A or ZERO
  local baseline_len="$4"      # baseline file size (string like 256k or 262145)
  local write_style="$5"       # overwrite or append
  local off="$6"
  local sz="$7"

  mkdir -p "$(dirname "$file")"

  # prep baseline: always truncate -s (加密组你要求只用 truncate -s 准备阶段也满足)
  truncate -s "$baseline_len" "$file"

  # baseline content
  if [[ "$baseline_kind" == "A" ]]; then
    fill_A "$file" "$baseline_len"
    sync
    drop_caches_3
  fi

  # compute append offset if needed
  if [[ "$write_style" == "append" ]]; then
    off="$baseline_len"
  fi

  echo
  echo "================================================================"
  echo "[RUN] $tag"
  stat "$file"
  echo "      baseline_kind=$baseline_kind baseline_len=$baseline_len"
  echo "      write_style=$write_style  offset=$off  size=$sz"
  echo "================================================================"

  # write pattern
  write_pattern "$file" "$off" "$sz"

  # expected file length after write
  # overwrite: expected_len = baseline_len (我们保证 off+sz <= baseline_len)
  # append: expected_len = baseline_len + sz
  local expected_len
  if [[ "$write_style" == "append" ]]; then
    # baseline_len is a size string; convert using python quickly
    expected_len="$(python3 - "$baseline_len" "$sz" <<'PY'
import sys
def parse_bytes(s):
    s=s.strip().lower()
    u=s[-1]
    if u.isalpha():
        num=s[:-1]; f={'b':1,'k':1024,'m':1024**2,'g':1024**3}[u]
    else:
        num=s; f=1
    return int(num)*f
print(parse_bytes(sys.argv[1])+parse_bytes(sys.argv[2]))
PY
)"

  else
    expected_len="$(python3 - "$baseline_len" <<'PY'
import sys
def parse_bytes(s):
    s=s.strip().lower()
    u=s[-1]
    if u.isalpha():
        num=s[:-1]; f={'b':1,'k':1024,'m':1024**2,'g':1024**3}[u]
    else:
        num=s; f=1
    return int(num)*f
print(parse_bytes(sys.argv[1]))
PY
)"
  fi

  # convert baseline_len/off/sz to int for verifier
  local baseline_len_i off_i sz_i
  baseline_len_i="$(python3 - "$baseline_len" <<'PY'
import sys
def parse_bytes(s):
    s=s.strip().lower()
    u=s[-1]
    if u.isalpha():
        num=s[:-1]; f={'b':1,'k':1024,'m':1024**2,'g':1024**3}[u]
    else:
        num=s; f=1
    return int(num)*f
print(parse_bytes(sys.argv[1]))
PY
)"
  off_i="$(python3 - "$off" <<'PY'
import sys
def parse_bytes(s):
    s=s.strip().lower()
    u=s[-1]
    if u.isalpha():
        num=s[:-1]; f={'b':1,'k':1024,'m':1024**2,'g':1024**3}[u]
    else:
        num=s; f=1
    return int(num)*f
print(parse_bytes(sys.argv[1]))
PY
)"
  sz_i="$(python3 - "$sz" <<'PY'
import sys
def parse_bytes(s):
    s=s.strip().lower()
    u=s[-1]
    if u.isalpha():
        num=s[:-1]; f={'b':1,'k':1024,'m':1024**2,'g':1024**3}[u]
    else:
        num=s; f=1
    return int(num)*f
print(parse_bytes(sys.argv[1]))
PY
)"

  # full-file cold verify
  full_verify_disk "$file" "$baseline_kind" "$baseline_len_i" "$off_i" "$sz_i" "$expected_len"

  echo "[OK ] $tag"
}

# ==============================
# 组装矩阵
# ==============================

echo
echo "##############################"
echo "# A) EXISTING FILE with content 'A' : overwrite + append"
echo "##############################"

# # A1 普通文件 existing(A) overwrite
# for item in "${OVERWRITE_CASES[@]}"; do
#   IFS='|' read -r name base off sz <<< "$item"
#   f="$PLAIN_DIR/plain_existA_overwrite_${name}.bin"
#   run_one "plain/existA/overwrite/${name}" "$f" "A" "$base" "overwrite" "$off" "$sz"
# done

# # A2 普通文件 existing(A) append
# for item in "${APPEND_CASES[@]}"; do
#   IFS='|' read -r name base sz <<< "$item"
#   f="$PLAIN_DIR/plain_existA_append_${name}.bin"
#   run_one "plain/existA/append/${name}" "$f" "A" "$base" "append" "0" "$sz"
# done

# A3 加密文件 existing(A) overwrite（准备阶段 truncate -s，内容由 fill_A 写入）
for item in "${OVERWRITE_CASES[@]}"; do
  IFS='|' read -r name base off sz <<< "$item"
  f="$ENC_DIR/enc_existA_overwrite_${name}.bin"
  run_one "enc/existA/overwrite/${name}" "$f" "A" "$base" "overwrite" "$off" "$sz"
done

# A4 加密文件 existing(A) append
for item in "${APPEND_CASES[@]}"; do
  IFS='|' read -r name base sz <<< "$item"
  f="$ENC_DIR/enc_existA_append_${name}.bin"
  run_one "enc/existA/append/${name}" "$f" "A" "$base" "append" "0" "$sz"
done

echo
echo "##############################"
echo "# B) HOLE/SPARSE FILE (all zeros) : overwrite + append"
echo "##############################"

# B1 普通 hole overwrite（baseline=0）
for item in "${OVERWRITE_CASES[@]}"; do
  IFS='|' read -r name base off sz <<< "$item"
  f="$PLAIN_DIR/plain_hole_overwrite_${name}.bin"
  run_one "plain/hole/overwrite/${name}" "$f" "ZERO" "$HOLE_A" "overwrite" "$off" "$sz"
done

# B2 普通 hole append（用 aligned/unaligned hole size 做 offset 对齐变化）
for item in "${APPEND_CASES[@]}"; do
  IFS='|' read -r name base sz <<< "$item"
  # 这里把 base 复用为 hole baseline size（aligned / unaligned）
  # base 是 BASE_A/BASE_U 字符串，刚好也可用作 HOLE_A/HOLE_U
  f="$PLAIN_DIR/plain_hole_append_${name}.bin"
  run_one "plain/hole/append/${name}" "$f" "ZERO" "$base" "append" "0" "$sz"
done

# B3 加密 hole overwrite
for item in "${OVERWRITE_CASES[@]}"; do
  IFS='|' read -r name base off sz <<< "$item"
  f="$ENC_DIR/enc_hole_overwrite_${name}.bin"
  run_one "enc/hole/overwrite/${name}" "$f" "ZERO" "$HOLE_A" "overwrite" "$off" "$sz"
done

# B4 加密 hole append（同理：aligned/unaligned hole size）
for item in "${APPEND_CASES[@]}"; do
  IFS='|' read -r name base sz <<< "$item"
  f="$ENC_DIR/enc_hole_append_${name}.bin"
  run_one "enc/hole/append/${name}" "$f" "ZERO" "$base" "append" "0" "$sz"
done

echo
echo "==================== ALL CASES PASSED ===================="
echo "PLAIN files under: $PLAIN_DIR"
echo "ENC   files under: $ENC_DIR"
echo "=========================================================="
