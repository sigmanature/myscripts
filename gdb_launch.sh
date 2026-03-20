#!/bin/bash

# GDB 可执行文件
GDB="gdb-multiarch"

# 内核符号文件
VMLINUX="./f2fs_head/vmlinux"

# 检查 vmlinux 文件是否存在
if [ ! -f "$VMLINUX" ]; then
    echo "错误: vmlinux 文件未在当前目录找到!"
    exit 1
fi

# 使用 -ex 参数链式执行 GDB 命令
# -tui: 开启文本界面
# -ex "target remote ...": 连接到 QEMU
# GDB 会在执行完所有 -ex 命令后停下来，等待用户输入
$GDB -tui "$VMLINUX" \
    -ex "target remote localhost:1234" \
    -ex "directory ."
    # 你可以继续添加更多 -ex 命令，比如 -ex "b start_kernel"

echo "GDB TUI 已启动并连接到 QEMU。请输入 GDB 命令 (如 'c' 继续执行)..."
