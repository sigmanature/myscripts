#!/usr/bin/env bash
# 列出 OLD..NEW 之间的新增函数，并把 “name+signature” 写入 new_func_list.txt
# 用法: ./list_new_funcs.sh [OLD] [NEW] [OUTFILE]
# 默认 OLD=HEAD~1  NEW=HEAD  OUTFILE=new_func_list.txt
set -euo pipefail

OLD=${1:-HEAD~1}
NEW=${2:-HEAD}
OUT=${3:-new_func_list.txt}

# 关心的文件：.c/.h，排除 mm/
mapfile -t FILES < <(
  git diff --name-only "$OLD" "$NEW" -- '*.c' '*.h' ':(exclude)mm/*'
)
((${#FILES[@]})) || { echo "❗️没有符合条件的改动文件"; : >"$OUT"; exit 0; }

ODIR=$(mktemp -d); NDIR=$(mktemp -d)
trap 'rm -rf "$ODIR" "$NDIR" "$TMP"' EXIT
for f in "${FILES[@]}"; do
  mkdir -p "$ODIR/$(dirname "$f")" "$NDIR/$(dirname "$f")"
  git show "$OLD:$f" >"$ODIR/$f" 2>/dev/null || true
  git show "$NEW:$f" >"$NDIR/$f" 2>/dev/null || true
done

collect() {            # $1=dir   >> stdout: file|name|sig
  ( cd "$1"
    # 签名字段：universal-ctags 有 (+S)，老 ctags 没有也不碍事
    ctags -R -f - --kinds-C=f --fields=+S --sort=no . 2>/dev/null \
    || ctags  -R -f - --kinds-C=f --sort=no  . 2>/dev/null
  ) | awk -F'\t' '
        {
          name=$1; file=$2; sig=""
          for(i=4;i<=NF;i++) if($i~/^signature:/){ sub(/^signature:/,"",$i); sig=$i }
          printf "%s|%s|%s\n", file, name, sig
        }'
}

declare -A OLD NEW
while IFS='|' read -r file name sig; do OLD["$file|$name"]="$sig"; done < <(collect "$ODIR")
while IFS='|' read -r file name sig; do NEW["$file|$name"]="$sig"; done < <(collect "$NDIR")

TMP=$(mktemp)

echo "========== 新增函数 =========="
for k in "${!NEW[@]}"; do
  [[ -n ${OLD[$k]+_} ]] && continue          # 旧版已有 → 不是新增
  file=${k%%|*}; name=${k#*|}; sig=${NEW[$k]}

  # 生成“无额外空格”的串
  if [[ -z $sig ]]; then
    combo="${name}()"
  elif [[ $sig == \(* ]]; then
    combo="${name}${sig}"
  else
    combo="$sig"          # sig 已包含返回值+名字
  fi

  printf "%s\n"  "$combo"              >>"$TMP"
  printf "%-40s [%s]\n" "$combo" "$file"   # 控制台
done | sort

sort -u "$TMP" >"$OUT"
echo "➡️  新增函数列表已写入: $(pwd)/$OUT"
