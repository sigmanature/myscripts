# var.sh 内容（请替换为你实际的路径和UUID）
#!/bin/bash

# 基础路径（根据你的实际环境修改）
export BASE="/home/nzzhao/learn_os"          # 根目录
export IMG_BASE="${BASE}/images"                # 镜像文件存放目录
export SCRIPT="${BASE}/myscripts"

# 各镜像的UUID（关键！用 blkid 命令查你对应的img文件）
export ROOT_IMG_UUID="4a125016-122e-4996-b388-ee85c127453d"       # ubuntu.img的UUID（可选，根系统用vda不变）
export F2FS_IMG_UUID="fa9a5e62-029a-41fd-9648-c3b34f5d176e"   # f2fs_com.img的UUID（重点）

# 默认内核目录（可选，脚本里也有默认，但这里集中管理）
export DEFAULT_KERDIR="${BASE}/f2fs_upstream"
# 默认内存
export DEFAULT_MEM="8184M"