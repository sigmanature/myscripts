#!/bin/bash

# ==============================================================================
# 脚本名称: manage_image.sh
# 脚本功能: 挂载或卸载一个磁盘镜像文件到回环设备。
# 使用方法:
#   - 挂载: sudo ./manage_image.sh mount
#   - 卸载: sudo ./manage_image.sh umount
# ==============================================================================

# --- 配置区 ---
# 请在这里修改你的镜像文件名和期望的挂载点路径
IMAGE_FILE="./lff2fs.img"
MOUNT_POINT="./f2rootfs"
# --- 配置区结束 ---


# 脚本在遇到任何错误时立即退出
set -e

# --- 函数定义 ---

# 显示使用方法的函数
usage() {
    echo "错误: 无效的参数。"
    echo "使用方法: $0 mount | umount"
    exit 1
}

# 挂载逻辑的函数
do_mount() {
    echo "--- 开始挂载流程 ---"

    # 1. 检查镜像文件是否存在
    if [ ! -f "$IMAGE_FILE" ]; then
        echo "错误: 镜像文件 '$IMAGE_FILE' 不存在！"
        exit 1
    fi

    # 2. 检查挂载点是否已经被占用
    if mountpoint -q "$MOUNT_POINT"; then
        echo "提示: '$MOUNT_POINT' 已经挂载了设备。"
        echo "挂载信息如下:"
        findmnt --target "$MOUNT_POINT"
        echo "无需重复挂载。"
        exit 0
    fi

    # 3. 创建挂载点目录（如果不存在）
    echo "检查并创建挂载点: $MOUNT_POINT"
    mkdir -p "$MOUNT_POINT"

    # 4. 查找一个可用的回环设备，并将镜像文件与之关联
    # losetup -fP --show 会自动找到可用设备、关联文件并打印设备名
    echo "正在将 '$IMAGE_FILE' 关联到回环设备..."
    LOOP_DEV=$(losetup -fP --show "$IMAGE_FILE")
    if [ -z "$LOOP_DEV" ]; then
        echo "错误: 无法关联回环设备！"
        exit 1
    fi
    echo "成功关联到: $LOOP_DEV"

    # 5. 挂载回环设备到挂载点
    echo "正在将 $LOOP_DEV 挂载到 $MOUNT_POINT..."
    mount -t f2fs "$LOOP_DEV" "$MOUNT_POINT"

    echo ""
    echo "✅ 挂载成功！"
    echo "镜像 '$IMAGE_FILE' 已挂载到 '$MOUNT_POINT'."
    echo "你可以通过以下命令查看内容:"
    echo "ls -l $MOUNT_POINT"
    echo "----------------------"
}

# 卸载逻辑的函数
do_umount() {
    echo "--- 开始卸载流程 ---"

    # 1. 检查挂载点是否真的被挂载了
    if ! mountpoint -q "$MOUNT_POINT"; then
        echo "提示: '$MOUNT_POINT' 当前未挂载任何设备，无需卸载。"
        # 检查是否有残留的回环设备关联
        LOOP_DEV=$(losetup -l | grep "$IMAGE_FILE" | awk '{print $1}')
        if [ -n "$LOOP_DEV" ]; then
            echo "发现残留的回环设备 '$LOOP_DEV'，正在清理..."
            losetup -d "$LOOP_DEV"
        fi
        exit 0
    fi

    # 2. 找到与此挂载点关联的回环设备
    LOOP_DEV=$(findmnt -n -o SOURCE --target "$MOUNT_POINT")
    if [ -z "$LOOP_DEV" ]; then
        echo "警告: 无法找到与 '$MOUNT_POINT' 关联的设备，将尝试直接卸载。"
        umount "$MOUNT_POINT"
        echo "✅ '$MOUNT_POINT' 已卸载。"
        exit 0
    fi
    echo "找到关联设备: $LOOP_DEV"

    # 3. 卸载文件系统
    echo "正在从 '$MOUNT_POINT' 卸载文件系统..."
    umount "$MOUNT_POINT"

    # 4. 分离回环设备
    echo "正在分离回环设备 '$LOOP_DEV'..."
    losetup -d "$LOOP_DEV"

    # 5. 删除挂载点目录（可选，但推荐）
    echo "正在删除空的挂载点目录 '$MOUNT_POINT'..."
    if [ -d "$MOUNT_POINT" ]; then
        rmdir "$MOUNT_POINT"
    fi

    echo ""
    echo "✅ 卸载成功！"
    echo "所有相关资源已清理。"
    echo "--------------------"
}


# --- 主逻辑 ---

# 检查是否以 root 权限运行
if [ "$(id -u)" -ne 0 ]; then
    echo "错误: 此脚本需要 root 权限运行。"
    echo "请尝试使用: sudo $0 $1"
    exit 1
fi

# 根据第一个参数（$1）来决定执行哪个操作
case "$1" in
    mount)
        do_mount
        ;;
    umount)
        do_umount
        ;;
    *)
        usage
        ;;
esac

exit 0
