#!/bin/bash

if [ -z "${1}" ]; then
    echo "Usage: $0 CHROOT_DIR [BRANCH_NAME|umount]"
    exit 1
fi

chrootdir="${1}"
branchname="${2:-}"

if [ ! -d "${chrootdir}" ]; then
    echo "Not dir ${chrootdir}"
    exit 1
fi

if [ "$(df -T "${chrootdir}" | tail -n1 | awk '{print $2}')" = "sffs" ]; then
    echo "Should not use sffs"
    exit 1
fi

function mountfs()
{
    mountpoint -q "${chrootdir}/dev" || mount --bind /dev "${chrootdir}/dev"
    mountpoint -q "${chrootdir}/dev/pts" || mount -t devpts pts "${chrootdir}/dev/pts"
    mountpoint -q "${chrootdir}/proc" || mount -t proc proc "${chrootdir}/proc"
    mountpoint -q "${chrootdir}/sys" || mount -t sysfs sys "${chrootdir}/sys"
}

function umountfs()
{
    mountpoint -q "${chrootdir}/dev/pts" && umount -lf "${chrootdir}/dev/pts"
    mountpoint -q "${chrootdir}/dev" && umount -lf "${chrootdir}/dev"
    mountpoint -q "${chrootdir}/proc" && umount -lf "${chrootdir}/proc"
    mountpoint -q "${chrootdir}/sys" && umount -lf "${chrootdir}/sys"
}

function chroot_run()
{
    env -i - LC_ALL=C.UTF-8 LANG=en_US.UTF-8 LANGUAGE=en_US: LROOT=/root TERM=${TERM} USER=root TZ=Asia/Shanghai HOME=/root _=/usr/bin/env /usr/sbin/chroot "${chrootdir}" "$@"
    #env 命令被执行 (在父进程中): 当你输入 env -i ... command ... 并按下回车键时，你的 shell (例如 bash) 会首先解析这个命令，并准备执行 env 命令。 此时，env 命令本身是在你的 当前 shell 进程 (父进程) 中开始运行的。

    #env 命令创建子进程: env 命令的核心操作之一就是使用系统调用 (例如 fork()) 
    # 创建一个新的进程，这个新进程就是 子进程。 这个子进程会 复制 父进程 (也就是你的 shell 进程) 的一些信息，例如代码段、数据段、堆栈等。

    #env 命令修改子进程的环境: 在子进程被创建之后，env 命令会根据你提供的选项和参数，
    # 修改这个子进程的环境变量。 如果使用了 -i 选项，env 会 清空子进程的环境变量，然后再根据你指定的 NAME=VALUE 对，设置新的环境变量。 如果没有使用 -i，env 通常会 继承父进程的环境变量，然后再进行修改 (如果指定了 NAME=VALUE 对)。

    #env 命令在子进程中执行指定的命令: 在子进程的环境变量被设置好之后，env 命令会使用系统调用 (例如 execve())，
    # 在 子进程中 执行你指定的 command (以及 command 的参数 ARG 等)。 execve() 系统调用会用新的程序代码 替换 当前子进程的代码段、数据段、堆栈等，从而使子进程开始执行新的程序 (也就是你指定的 command)。
    #子进程执行完毕后，控制权返回给父进程 (shell): 当子进程执行完毕 (无论是正常退出还是异常终止) 后，子进程会退出，
    # 并将退出状态返回给父进程 (env 命令)。 env 命令自身也会退出，并将子进程的退出状态传递给它的父进程 (也就是你的 shell 进程)。 最终，你的 shell 进程会重新获得控制权，等待你输入新的命令。
    # env -i 清空环境变量 接下来的大写字母全是环境变量
    # LC_ALL=C.UTF-8 LANG=en_US.UTF-8 LANGUAGE=en_US:: 设置 locale 相关环境变量，通常设置为 UTF-8 编码的英文环境。
    # LROOT=/root: 自定义环境变量 LROOT 设置为 /root，用途可能在 chroot 环境内部的脚本中使用。
    # TERM=${TERM}: 传递宿主机的终端类型。
    # USER=root: 设置用户为 root。
    # TZ=Asia/Shanghai: 设置时区为 Asia/Shanghai。
    # HOME=/root: 设置家目录为 /root。
    # _=/usr/bin/env: _ 环境变量通常设置为最后执行的命令路径。
    # /usr/sbin/chroot "${chrootdir}" "$@": 执行 chroot 命令，将根目录切换到 ${chrootdir}，
    # 并将 chroot_run 函数接收到的所有参数 ($@) 传递给 chroot 内部执行的命令。
}

if [ "${branchname}" = "umount" ]; then
    umountfs
    exit 0
fi

if [ ! -s "${chrootdir}/etc/hosts" ]; then
    cat >"${chrootdir}/etc/hosts" << EOF
127.0.0.1       localhost
EOF
fi

if [ ! -s "${chrootdir}/etc/resolv.conf" ]; then
    cat >"${chrootdir}/etc/resolv.conf" << EOF
nameserver 114.114.114.114
nameserver 8.8.8.8
EOF
fi

mountfs

echo "Enter chroot dir ${chrootdir}"
chroot_run bash -c "cd /root; exec bash --login -i"