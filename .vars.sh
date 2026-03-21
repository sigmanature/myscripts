# var.sh 内容（请替换为你实际的路径和UUID）
#!/bin/bash

# 基础路径（根据你的实际环境修改）
export BASE="/home/nzzhao/learn_os"          # 根目录
export IMG_BASE="${BASE}/images"                # 镜像文件存放目录
export SCRIPT="${BASE}/myscripts"
export SHARE="${SCRIPT}/shared_with_qemu"
export TEST="${SHARE}/test"

# 默认内核目录（可选，脚本里也有默认，但这里集中管理）
export DEFAULT_KERDIR="${BASE}/f2fs_upstream"
# 默认内存
export DEFAULT_MEM="8184M"

kobj() {
  local src_root=$BASE/f2fs
  local out_root=$BASE/f2fs_upstream
  local arch=arm64
  local cross=aarch64-linux-gnu-

  local input="$1"
  local target="${input%.c}.o"

  make -C "$src_root" \
    O="$out_root" \
    ARCH="$arch" \
    CROSS_COMPILE="$cross" \
    olddefconfig >/dev/null

  make -C "$src_root" \
    O="$out_root" \
    ARCH="$arch" \
    CROSS_COMPILE="$cross" \
    V=1 \
    "$target"
}