#!/bin/bash
#
# f2fs GC 测试 (v3):
# - 全自动：在当前系统里创建一个 f2fs 镜像文件，通过 loop 设备挂载
# - 构造一个 segment，其中少量 immutable 数据块 + 大量普通文件数据块
# - 删除普通文件后，通过 f2fs_io gc 触发 GC，观察 immutable 文件数据块是否被搬迁
#

set -euo pipefail

# ===================== 配置区 =====================

# 测试使用的镜像文件（放在 WORK 目录里）
WORK="${WORK:-/tmp/f2fs_gc_immutable}"
IMG="${IMG:-$WORK/f2fs_gc_test.img}"

# 镜像大小（MiB），可以通过 IMG_MB=128 ./xxx.sh 来改
IMG_MB_DEFAULT=80
IMG_MB="${IMG_MB:-$IMG_MB_DEFAULT}"

# 这里假定 f2fs 的 segment 大小为 2 MiB (512 * 4KiB)
SEG_BYTES="${SEG_BYTES:-$((2 * 1024 * 1024))}"

# immutable 在一个 segment 里占的比例：1 / IMM_FRAC
# 默认：immutable 占 1/8，一个段里大约 12.5% immutable，87.5% 其他文件。
IMM_FRAC="${IMM_FRAC:-8}"

# 是否 dump 全部 SIT/SSA（默认关，开了会多出几个大文件）
DUMP_SITSSA="${DUMP_SITSSA:-0}"

# 挂载点
MNT="${MNT:-/mnt/f2fs_gc_test}"

# loop 设备在运行时生成
LOOPDEV=""

# =================================================

echo "[+] 工作目录        : $WORK"
echo "[+] 镜像文件        : $IMG"
echo "[+] 镜像大小        : ${IMG_MB} MiB"
echo "[+] segment 大小    : $SEG_BYTES bytes"
echo "[+] immutable 比例  : 1/${IMM_FRAC} (约 $(awk "BEGIN{printf \"%.1f\",100/$IMM_FRAC}")%)"

need_cmds=(
    mkfs.f2fs mount umount chattr filefrag fallocate f2fs_io dump.f2fs \
    stat dd awk losetup
)

echo "[1/8] 检查依赖工具..."
for c in "${need_cmds[@]}"; do
    if ! command -v "$c" >/dev/null 2>&1; then
        echo "ERROR: 未找到命令: $c"
        echo "请先安装相关工具，例如:"
        echo "  apt-get install -y f2fs-tools e2fsprogs util-linux"
        exit 1
    fi
done

# 清理函数：确保不留挂载点和 loop 设备
cleanup() {
    set +e
    echo "[CLEANUP] 开始清理..."
    if mountpoint -q "$MNT"; then
        echo "  - umount $MNT"
        umount "$MNT"
    fi
    if [ -n "$LOOPDEV" ] && losetup -a | grep -q "^$LOOPDEV:"; then
        echo "  - losetup -d $LOOPDEV"
        losetup -d "$LOOPDEV"
    fi
    # if [ -d "$WORK" ]; then
    #     echo "  - 删除 $WORK"
    #     rm -rf "$WORK"
    # fi
}
trap cleanup EXIT

# 计算这个“混合 segment”里 immutable 和其他文件的大小
IMM_SIZE=$((SEG_BYTES / IMM_FRAC))                 # immutable 大小
MIX_OTHER_SIZE=$((SEG_BYTES - IMM_SIZE))           # 同一个 segment 内其他文件的大小

BLK_SIZE=4096
IMM_BLOCKS=$((IMM_SIZE / BLK_SIZE))
MIX_OTHER_BLOCKS=$((MIX_OTHER_SIZE / BLK_SIZE))

echo "[+] 目标：一个 segment 里 immutable ~${IMM_SIZE} bytes，其它 ~${MIX_OTHER_SIZE} bytes"
echo "    immutable 块数 : ${IMM_BLOCKS} (4KiB block)"
echo "    其他块数       : ${MIX_OTHER_BLOCKS} (4KiB block)"

# ===================== 准备工作目录和镜像 =====================

echo "[2/8] 准备工作目录和镜像文件..."
rm -rf "$WORK"
mkdir -p "$WORK"
mkdir -p "$MNT"

IMG_BYTES=$((IMG_MB * 1024 * 1024))

echo "  - 创建 ${IMG_MB}MiB 镜像文件: $IMG"
# 用 fallocate 比 dd 快；某些环境不用 sparse，可以替换成 dd
rm -f "$IMG"
fallocate -l "$IMG_BYTES" "$IMG"

# 绑成 loop 设备
echo "  - 绑定 loop 设备..."
LOOPDEV=$(losetup --find --show "$IMG")
echo "    loop 设备: $LOOPDEV"

# ===================== 格式化为 f2fs =====================

echo "[3/8] 在 $LOOPDEV 上创建新的 f2fs（segs_per_sec = 1）..."
# -s 1: 一个 section 只包含 1 个 segment，这样 GC 的单位就是段本身
mkfs.f2fs -f -l f2fs_gc_test -s 1 "$LOOPDEV" >/dev/null

echo "  - 挂载 f2fs（通过 LABEL=f2fs_gc_test，关闭 background_gc）..."
mount -t f2fs -o background_gc=off,discard LABEL=f2fs_gc_test "$MNT"

# ===================== 构造 workload =====================

echo "[4/8] 构造 workload：先写一些“长寿命冷数据”，再构造混合 segment..."

cd "$MNT"
rm -rf ./*

# 4.1 写一些“冷数据”文件，占据若干个 segment，但以后不删除，让它们保持 100% 有效
echo "  - 创建若干冷数据文件（一直保留，不参与 GC）..."
for i in $(seq 1 3); do
    # 每个 2MiB，大概 1 个 segment（视实际 segment size）
    fallocate -l 2M "cold_valid_${i}.dat"
done
sync

# 4.2 构造“混合 segment”：immutable + 其他普通文件在同一个 segment 里
echo "  - 构造混合 segment：immutable.dat + mix_garbage.dat"

echo "    * 写 immutable.dat（先写少量数据，占 segment 小部分）..."
dd if=/dev/zero of=immutable.dat bs=$BLK_SIZE count=$IMM_BLOCKS conv=fsync >/dev/null 2>&1

IMM_INO=$(stat -c '%i' immutable.dat)
echo "      immutable inode: $IMM_INO"

echo "    * 写 mix_garbage.dat（紧接着写，填满这个 segment 的剩余部分）..."
dd if=/dev/zero of=mix_garbage.dat bs=$BLK_SIZE count=$MIX_OTHER_BLOCKS conv=fsync >/dev/null 2>&1

sync

# 4.3 再追加一些长寿命文件，让盘有一定使用率，防止 GC 总是挑全空的段
echo "  - 再追加少量冷数据文件，以提升整体使用率..."
for i in $(seq 1 4); do
    fallocate -l 2M "cold_tail_${i}.dat"
done
sync

# 4.4 记录 GC 前的 immutable 布局
echo "  - 记录 GC 前 immutable.dat 的布局信息..."

filefrag -v immutable.dat > "$WORK/immutable_before.filefrag" 2>&1 || true
tail -n +4 "$WORK/immutable_before.filefrag" > "$WORK/immutable_before.extents" || true

# dump.f2fs -i 只针对 immutable inode，方便你之后按块号算 segment/比例
echo n | dump.f2fs -i "$IMM_INO" "$LOOPDEV" > "$WORK/dump_immutable_before.txt" 2>&1 || true

if [ "$DUMP_SITSSA" -eq 1 ]; then
    echo "  - (可选) dump 全部 SIT/SSA（可能稍大）..."
    dump.f2fs -s 0 -1 "$LOOPDEV" > "$WORK/dump_sit_before.txt" 2>&1 || true
    dump.f2fs -a 0 -1 "$LOOPDEV" > "$WORK/dump_ssa_before.txt" 2>&1 || true
fi

echo "  - 将 immutable.dat 设为 immutable（只读）..."
chattr +i immutable.dat

# 4.5 删除混合段里的“垃圾部分”，使该 segment 大部分无效、少量有效（immutable）
echo "  - 删除 mix_garbage.dat，制造一个“少量有效块 + 大量无效块”的 segment..."
rm -f mix_garbage.dat
sync


# ===================== 手动触发 GC 并观察 immutable 块是否被搬迁 =====================

echo "[5/8] 使用 f2fs_io gc 触发 GC，观察 immutable.dat 的物理布局是否变化..."

changed=0
max_rounds=20

for i in $(seq 1 "$max_rounds"); do
    echo "  - GC round $i ..."
    # sync_mode=1, path=挂载点
    if ! f2fs_io gc 1 "$MNT" 2>>"$WORK/gc.log"; then
        echo "    (f2fs_io gc 返回非 0，忽略，详细见 $WORK/gc.log)"
    fi
    sync

    filefrag -v immutable.dat > "$WORK/immutable_after_round_${i}.filefrag" 2>&1 || true
    tail -n +4 "$WORK/immutable_after_round_${i}.filefrag" > "$WORK/immutable_after.extents" || true

    if ! diff -q "$WORK/immutable_before.extents" "$WORK/immutable_after.extents" >/dev/null 2>&1; then
        echo "    -> 检测到 immutable.dat 物理布局发生变化（GC 已搬迁其数据块）"
        changed=1

        # 做一次 dump.f2fs -i，记录 GC 后的 inode 布局
        dump.f2fs -i "$IMM_INO" "$LOOPDEV" > "$WORK/dump_immutable_after.txt" 2>&1 || true

        # if [ "$DUMP_SITSSA" -eq 1 ]; then
        #     dump.f2fs -s 0 -1 "$LOOPDEV" > "$WORK/dump_sit_after.txt" 2>&1 || true
        #     dump.f2fs -a 0 -1 "$LOOPDEV" > "$WORK/dump_ssa_after.txt" 2>&1 || true
        # fi
        break
    else
        echo "    -> 本轮 GC 后 immutable.dat 布局未变，继续尝试..."
    fi
done

# ===================== 收尾 =====================

echo "[6/8] 卸载文件系统..."
cd /
if mountpoint -q "$MNT"; then
    umount "$MNT"
fi

echo "[7/8] 释放 loop 设备..."
if [ -n "$LOOPDEV" ] && losetup -a | grep -q "^$LOOPDEV:"; then
    losetup -d "$LOOPDEV"
fi

echo "[8/8] 测试结果汇总..."

if [ "$changed" -eq 1 ]; then
    echo "============================================================"
    echo "PASS: 在 $max_rounds 轮 GC 内，检测到 immutable.dat 的物理块布局变化。"
    echo "      说明存在这样一个场景：某段中少量 immutable 块 + 大量普通块，"
    echo "      当普通块被淘汰并触发 GC 时，immutable 块被搬迁到了新位置。"
    echo ""
    echo "  - filefrag 前后结果："
    echo "      $WORK/immutable_before.filefrag"
    echo "      $WORK/immutable_after_round_${i}.filefrag"
    echo ""
    echo "  - dump.f2fs immutable inode："
    echo "      $WORK/dump_immutable_before.txt"
    echo "      $WORK/dump_immutable_after.txt"
    if [ "$DUMP_SITSSA" -eq 1 ]; then
        echo "  - SIT/SSA dump:"
        echo "      $WORK/dump_sit_before.txt / dump_sit_after.txt"
        echo "      $WORK/dump_ssa_before.txt / dump_ssa_after.txt"
    fi
    echo "============================================================"
    exit 0
else
    echo "============================================================"
    echo "FAIL: 在 $max_rounds 轮 GC 内，immutable.dat 的物理块布局没有变化。"
    echo "      可能原因："
    echo "        * 该混合 segment 并未被选为 GC victim；"
    echo "        * 当前 f2fs 实际 segment 大小或写入模式与假设不同。"
    echo ""
    echo "你可以参考以下方向调试："
    echo "  - 调整 SEG_BYTES 或 IMM_FRAC，使混合比例更极端一点；"
    echo "  - 调整 IMG_MB，使 segment 总数更少、更容易被挑中；"
    echo "  - 开启 DUMP_SITSSA=1，结合 dump_sit/dump_ssa 分析 victim 选择。"
    echo "============================================================"
    exit 1
fi