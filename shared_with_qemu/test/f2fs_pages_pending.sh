#!/bin/bash
#
# f2fs large-folio 极端碎片读测试
# - 自动创建 f2fs 镜像文件 -> loop -> mkfs.f2fs -> mount
# - 构造一个 immutable 文件：逻辑块 0..N-1 对应的物理块全部不相邻（extent 基本都是 1）
# - drop_caches 后顺序读，触发 read/readahead 路径
#
set -euo pipefail

# ===================== 配置区 =====================
# 建议放 /dev/shm（内存盘）让 bio 完成更快；没有就用 /tmp
WORK="${WORK:-/dev/shm/f2fs_lfolio_frag}"
IMG="${IMG:-$WORK/f2fs_lfolio_frag.img}"
MNT="${MNT:-/mnt/f2fs_lfolio_frag}"

IMG_MB_DEFAULT=256
IMG_MB="${IMG_MB:-$IMG_MB_DEFAULT}"


# loop 设备 readahead（KiB）。调大一点，利于 readahead 路径生成更大 folio
READAHEAD_KB="${READAHEAD_KB:-4096}"

LOOPDEV=""

need_cmds=(
  mkfs.f2fs mount umount losetup chattr filefrag dd fallocate blockdev sync awk
)

# ===================== 前置检查 =====================
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "ERROR: 需要 root 运行（mount/losetup/drop_caches）。"
  exit 1
fi

echo "[+] WORK=$WORK"
echo "[+] IMG=$IMG (${IMG_MB} MiB)"
echo "[+] MNT=$MNT"
echo "[+] READAHEAD_KB=$READAHEAD_KB"

echo "[1/7] 检查依赖工具..."
for c in "${need_cmds[@]}"; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "ERROR: 未找到命令: $c"
    exit 1
  fi
done

# ===================== cleanup =====================
cleanup() {
  set +e
  echo "[CLEANUP] 开始清理..."
  if mountpoint -q "$MNT"; then
    echo "  - umount $MNT"
    umount "$MNT"
  fi
  if [ -n "${LOOPDEV:-}" ] && losetup -a | grep -q "^$LOOPDEV:"; then
    echo "  - losetup -d $LOOPDEV"
    losetup -d "$LOOPDEV"
  fi
}
trap cleanup EXIT

# ===================== 创建镜像 + loop =====================
echo "[2/7] 准备工作目录和镜像文件..."
rm -rf "$WORK"
mkdir -p "$WORK" "$MNT"

IMG_BYTES=$((IMG_MB * 1024 * 1024))
rm -f "$IMG"
fallocate -l "$IMG_BYTES" "$IMG"

echo "  - 绑定 loop 设备..."
LOOPDEV=$(losetup --find --show "$IMG")
echo "    loop: $LOOPDEV"

# ===================== mkfs + mount =====================
echo "[3/7] mkfs.f2fs + mount..."
mkfs.f2fs -f -l f2fs_lfolio_frag_test -s 1 "$LOOPDEV" >/dev/null

mount -t f2fs -o background_gc=off,discard LABEL=f2fs_lfolio_frag_test "$MNT"

# 提升 readahead
blockdev --setra "$((READAHEAD_KB * 2))" "$LOOPDEV" >/dev/null 2>&1 || true
echo "  - blockdev readahead set to ~${READAHEAD_KB}KiB (best-effort)"

# ===================== 构造极端碎片文件 =====================
echo "[4/7] 构造极端碎片 immutable 文件..."
cd "$MNT"
rm -rf ./*

TARGET="immutable_frag.dat"
FILLER="filler_interleave.dat"
BLOCKS="${BLOCKS:-640}"
echo "[+] BLOCKS_KB=每个文件大小 ~$(($BLOCKS * 4)) KiB)"
# 目标文件大小：BLOCKS * 4096
# 为了更容易形成 large folio + 多次 submit，建议至少几千块（比如 8192=32MiB）

# 写入策略：每写 TARGET 的 1 个 4KiB 块，就写 FILLER 的 1 个 4KiB 块
# 这样 TARGET 的物理块会呈现 X, X+2, X+4...（每块都不相邻 -> extent 多为 1）
#
# 为了尽量避免写回合并/重排：
# - 优先尝试 oflag=direct
# - 如果 direct 不支持就 fallback 到 conv=fdatasync（每次写都落盘）
#
write_one_block() {
  local file="$1" i="$2"
  if dd if=/dev/zero of="$file" bs=4096 count=1 seek="$i" oflag=direct,seek_bytes conv=notrunc status=none 2>/dev/null; then
    return 0
  fi
  dd if=/dev/zero of="$file" bs=4096 count=1 seek="$i" conv=notrunc,fdatasync status=none
}

echo "  - 交错写入 $BLOCKS blocks：$TARGET <-> $FILLER"
for ((i=0; i<BLOCKS; i++)); do
  write_one_block "$TARGET" "$i"
  write_one_block "$FILLER" "$i"
done
sync

# 删除 filler，但 target 的物理块依然保留“隔一个块”的布局（不相邻）
rm -f "$FILLER"
sync

echo "  - filefrag 检查碎片程度..."
filefrag -v "$TARGET" > "$WORK/filefrag_target.txt" 2>&1 || true

# 粗略统计 extent 行数（不同 filefrag 版本格式略不同，这里只是辅助参考）
EXTENTS=$(awk 'BEGIN{n=0} /^[ ]*[0-9]+:/{n++} END{print n}' "$WORK/filefrag_target.txt" || echo "?")
echo "    target extents(lines) ~= $EXTENTS"
echo "    结果保存: $WORK/filefrag_target.txt"

echo "  - 设为 immutable..."
chattr +i "$TARGET"

# ===================== drop_caches + 触发读 =====================
echo "[5/7] drop_caches，确保走真实读 IO..."
sync
echo 3 > /proc/sys/vm/drop_caches

echo "[6/7] 触发顺序读（多轮）..."
# 多轮读更容易撞到 timing 窗口
for r in $(seq 1 30); do
  dd if="$TARGET" of=/dev/null bs=1M status=none
done
echo "  - 读完成（如果你加了内核里的 delay，这里更容易触发）"

# ===================== 收尾（由 trap cleanup 完成） =====================
echo "[7/7] Done."
echo "  - filefrag 输出: $WORK/filefrag_target.txt"
echo "  - 挂载点: $MNT (脚本退出后会自动 umount + losetup -d)"
