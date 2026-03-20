#!/usr/bin/env bash
set -euo pipefail

OLD=${1:-HEAD~1}
NEW=${2:-HEAD}

# 只看 .c/.h，排除 mm/
mapfile -t FILES < <(
  git diff --name-only "$OLD" "$NEW" -- '*.c' '*.h' ':(exclude)mm/*'
)

if ((${#FILES[@]}==0)); then
  echo "没有符合条件的改动文件。"
  exit 0
fi

declare -A ADD MOD DEL

for f in "${FILES[@]}"; do
  git diff --no-color -U0 "$OLD" "$NEW" -- "$f" | \
  awk -v file="$f" '
    # 逐行读取，找到以 @@ 开头的 hunk 头
    /^@@/ {
      line=$0
      # 1) 去掉开头的 @@ 和一个空格
      sub(/^@@[[:space:]]*/,"", line)
      # 2) line 现在形如：-a[,b] +c[,d] @@ 余下上下文
      #    先取出上下文（可能为空）
      ctx=line
      sub(/^-?[0-9]+(,[0-9]+)?[[:space:]]+\+[0-9]+(,[0-9]+)?[[:space:]]+@@[[:space:]]*/, "", ctx)
      if (ctx=="") ctx="<unknown>"

      # 3) 取出 -a[,b] +c[,d] 这一段
      head=line
      sub(/[[:space:]]+@@.*/,"", head)

      # 4) 拆成两个字段：负号段 和 正号段
      #    例："-10,0 +12,7"
      split(head, parts, /[[:space:]]+/)
      oldspec=parts[1]   # -a[,b]
      newspec=parts[2]   # +c[,d]

      # 5) oldlen/newlen 解析
      gsub(/^-/, "", oldspec)
      gsub(/^\+/, "", newspec)

      oldlen=1; newlen=1
      # oldspec 可能是 "10" 或 "10,0"
      n=split(oldspec, a, ","); if (n==2) oldlen=a[2]+0
      n=split(newspec, b, ","); if (n==2) newlen=b[2]+0

      key = file "\t" ctx
      if (oldlen==0 && newlen>0)  add[key]=1
      else if (newlen==0 && oldlen>0) del[key]=1
      else                          mod[key]=1
    }
    END {
      for (k in add) print "ADD\t" k
      for (k in mod) print "MOD\t" k
      for (k in del) print "DEL\t" k
    }'
done | sort -u | awk -F'\t' '
  BEGIN{print "=== 函数体有改动的函数 ==="}
  $1=="ADD"{print "新增:  " $3 "  [" $2 "]"}
  $1=="MOD"{print "修改:  " $3 "  [" $2 "]"}
  $1=="DEL"{print "删除:  " $3 "  [" $2 "]"}
'
