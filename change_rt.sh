#!/bin/bash

# 定义你要检查的挂载点路径
MOUNT_POINT="./rootfs"  # 请替换成你实际的挂载点路径

# 定义你要挂载的设备或资源 (例如：硬盘分区, 网络共享等)
IMAGE_TO_MOUNT=$IMG_BASE/ubuntu.img       # 请替换成你实际的设备路径，例如 /dev/sdb1, //server/share 等
# bind mount 默认路径（可被外部环境变量覆盖）
KERNEL="${KERNEL:-$HOME/learn_os/f2fs_back_up}"
EBPF="${EBPF:-$HOME/learn_os/ebpf}"
# 可选：定义文件系统类型，如果可以自动检测，可以省略
FILESYSTEM_TYPE="ext4"           # 请替换成你实际的文件系统类型，例如 ext4, ntfs, cifs 等，如果可以自动检测可以留空

# 检查挂载点是否已经挂载
if mountpoint -q "$MOUNT_POINT"; then
  echo "路径 '$MOUNT_POINT' 已经挂载。"
else
  echo "路径 '$MOUNT_POINT' 未挂载，尝试挂载..."

  # 执行挂载命令
  if [ -n "$FILESYSTEM_TYPE" ]; then
    sudo mount -t "$FILESYSTEM_TYPE" "$IMAGE_TO_MOUNT" "$MOUNT_POINT"
  else
    sudo mount "$IMAGE_TO_MOUNT" "$MOUNT_POINT"
  fi

  # 检查挂载是否成功
  if [ $? -eq 0 ]; then
    echo "成功挂载到 '$MOUNT_POINT'。"
  else
    echo "挂载失败，请检查设备路径 '$IMAGE_TO_MOUNT' 和挂载点路径 '$MOUNT_POINT' 是否正确，以及是否有权限。"
    exit 1 # 脚本执行失败退出
  fi
fi

sudo mkdir -p "$MOUNT_POINT/mnt/kernel"
sudo mkdir -p "$MOUNT_POINT/mnt/ebpf"

if ! mountpoint -q "$MOUNT_POINT/mnt/kernel"; then
  sudo mount --bind "$KERNEL" "$MOUNT_POINT/mnt/kernel"
  echo "已 bind mount KERNEL -> $MOUNT_POINT/mnt/kernel"
fi

if ! mountpoint -q "$MOUNT_POINT/mnt/ebpf"; then
  sudo mount --bind "$EBPF" "$MOUNT_POINT/mnt/ebpf"
  echo "已 bind mount EBPF -> $MOUNT_POINT/mnt/ebpf"
fi

sudo bash ./chroot_mount.sh rootfs/ mount
