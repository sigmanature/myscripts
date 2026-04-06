#!/bin/bash
set -euo pipefail

# --- 1) 加载变量文件（读取 UUID / 路径 / 默认值）---
VAR_FILE="./.vars.sh"
if [[ ! -f "${VAR_FILE}" ]]; then
  echo "错误：找不到变量配置文件 ${VAR_FILE}"
  exit 1
fi
source "${VAR_FILE}" || {
  echo "错误：加载 ${VAR_FILE} 失败！"
  exit 1
}

# --- 2) 默认值（保持你原先的变量习惯）---
KERDIR="${DEFAULT_KERDIR}"
MEM="${DEFAULT_MEM}"

LOG_FILE="guest_console.log"        # 保留你原先的 guest_console.log
QMP_SOCK="/tmp/qemu-qmp.sock"       # 新增：QMP socket
QGA_SOCK="/tmp/qga.sock"
APPEND_EXTRA=""
FOREGROUND=0
PID_FILE=""
PID_FILE_AUTOGEN=0

usage() {
  cat <<EOF
用法:
  $0 [选项]

选项:
  -k, --kerdir DIR        指定内核目录 (默认: ${DEFAULT_KERDIR})
  -m, --mem SIZE          指定内存大小 (默认: ${DEFAULT_MEM})
  --qmp-sock PATH         指定 QMP unix socket (默认: ${QMP_SOCK})
  --qga-sock PATH         指定 QGA unix socket (默认: ${QGA_SOCK})
  --log FILE              指定 guest console 日志文件 (默认: ${LOG_FILE})
  --pid-file PATH         将 QEMU PID 写入指定文件
  --append-extra STR      追加额外 kernel cmdline 参数
  --foreground            前台运行，保留旧的 tee 诊断模式
  -h, --help              显示帮助
EOF
}

# --- 3) 参数解析 ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    -k|--kerdir) KERDIR="$2"; shift 2;;
    -m|--mem)    MEM="$2"; shift 2;;
    --qmp-sock)  QMP_SOCK="$2"; shift 2;;
    --qga-sock)  QGA_SOCK="$2"; shift 2;;
    --log)       LOG_FILE="$2"; shift 2;;
    --pid-file)  PID_FILE="$2"; shift 2;;
    --append-extra) APPEND_EXTRA="$2"; shift 2;;
    --foreground) FOREGROUND=1; shift;;
    -h|--help)   usage; exit 0;;
    *) echo "错误: 未知选项 '$1'"; usage; exit 1;;
  esac
done

echo "================ QEMU 配置 ================"
echo "Kernel Directory : ${KERDIR}"
echo "Memory Size      : ${MEM}"
echo "Console Log      : ${LOG_FILE}"
echo "QMP Socket       : ${QMP_SOCK}"
echo "QGA Socket       : ${QGA_SOCK}"
echo "Foreground Mode  : ${FOREGROUND}"
echo "Append Extra     : ${APPEND_EXTRA}"
echo "==========================================="

# --- 4) 保留你原来的 kernel cmdline（不改你的 console=ttyAMA0 语义）---
KERNEL_APPEND='panic=5 noinitrd root=/dev/vda rw console=ttyAMA0 nokaslr loglevel=8 ramoops.mem_address=0x1FF800000 ramoops.mem_size=0x200000 ramoops.record_size=0x20000 ramoops.console_size=0x20000 panic_on_oops=1 sysrq_always_enabled'
if [[ -n "${APPEND_EXTRA}" ]]; then
  KERNEL_APPEND="${KERNEL_APPEND} ${APPEND_EXTRA}"
fi

# stale unix sockets from a dead VM will block qemu startup.
rm -f "${QMP_SOCK}" "${QGA_SOCK}"
mkdir -p "$(dirname "${LOG_FILE}")"

QEMU_COMMON=(
  qemu-system-aarch64
  -smp 8
  -machine virt,virtualization=true,gic-version=3
  -m "size=${MEM}"
  -mem-prealloc
  -cpu cortex-a72
  -kernel "${KERDIR}/arch/arm64/boot/Image"
  -append "${KERNEL_APPEND}"
  -netdev "user,id=eth0,hostfwd=tcp::5022-:22,hostfwd=tcp::5080-:80"
  -device "virtio-net-device,netdev=eth0"
  -chardev "socket,path=${QGA_SOCK},server=on,wait=off,id=qga0"
  -device virtio-serial-pci
  -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0
  -drive "format=raw,file=${IMG_BASE}/ubuntu.img,if=virtio,id=rootdisk"
  -drive "format=raw,file=${IMG_BASE}/f2fs.img,if=virtio,id=f2fsnorm"
  -virtfs "local,path=${SCRIPT}/shared_with_qemu,mount_tag=hostshare,security_model=passthrough,id=hostshare"
  -s
  -qmp "unix:${QMP_SOCK},server=on,wait=off"
)

if [[ "${FOREGROUND}" -eq 1 ]]; then
  # Foreground mode is kept only for diagnosis, where full stdio visibility matters.
  "${QEMU_COMMON[@]}" -nographic 2>&1 | tee "${LOG_FILE}"
  exit $?
fi

if [[ -z "${PID_FILE}" ]]; then
  PID_FILE="$(mktemp /tmp/qemu-start-ori.pid.XXXXXX)"
  PID_FILE_AUTOGEN=1
fi
mkdir -p "$(dirname "${PID_FILE}")"
: > "${LOG_FILE}"
rm -f "${PID_FILE}"
"${QEMU_COMMON[@]}" \
  -display none \
  -monitor none \
  -serial "file:${LOG_FILE}" \
  -pidfile "${PID_FILE}" \
  -daemonize

if [[ ! -s "${PID_FILE}" ]]; then
  echo "错误: QEMU 启动后未生成 PID 文件 ${PID_FILE}" >&2
  exit 1
fi

QEMU_PID="$(cat "${PID_FILE}")"
if ! kill -0 "${QEMU_PID}" 2>/dev/null; then
  echo "错误: QEMU 启动失败，请检查 ${LOG_FILE}" >&2
  exit 1
fi

printf 'qemu_pid=%s\nconsole_log=%s\nqmp_sock=%s\nqga_sock=%s\n' \
  "${QEMU_PID}" "${LOG_FILE}" "${QMP_SOCK}" "${QGA_SOCK}"

if [[ "${PID_FILE_AUTOGEN}" -eq 1 ]]; then
  rm -f "${PID_FILE}"
fi
