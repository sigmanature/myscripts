#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VAR_FILE="${REPO_ROOT}/.vars.sh"
if [[ ! -f "${VAR_FILE}" ]]; then
  echo "错误：找不到变量配置文件 ${VAR_FILE}" >&2
  exit 1
fi
source "${VAR_FILE}" || {
  echo "错误：加载 ${VAR_FILE} 失败！" >&2
  exit 1
}

BASE_ROOT_IMG_DEFAULT="${QEMU_BASE_ROOT_IMG:-${IMG_BASE}/ubuntu.img}"
BASE_F2FS_IMG_DEFAULT="${QEMU_BASE_F2FS_IMG:-${IMG_BASE}/f2fs.img}"
INSTANCE_DIR_DEFAULT="${QEMU_INSTANCE_DIR:-${SCRIPT}/vm_instances}"
BASE_SHARE_DIR_DEFAULT="${QEMU_BASE_SHARE_DIR:-${SCRIPT}/shared_with_qemu}"
DEFAULT_SSH_PORT_BASE="${QEMU_SSH_PORT_BASE:-5022}"
DEFAULT_HTTP_PORT_BASE="${QEMU_HTTP_PORT_BASE:-5080}"

COMMAND="${1:-}"
INSTANCE_NAME="${2:-}"

usage() {
  cat <<EOF
QEMU 实例管理脚本

用法:
  $0 <命令> <实例名> [选项]

命令:
  start <实例名>    启动一个新的或已存在的虚拟机实例
  stop <实例名>     停止一个正在运行的虚拟机实例
  status <实例名>   输出实例元数据和运行状态
  cleanup <实例名>  停止实例并删除其所有文件

start 选项:
  -k, --kerdir DIR         指定内核目录 (默认: ${DEFAULT_KERDIR})
  -m, --mem SIZE           指定内存大小 (默认: ${DEFAULT_MEM})
  --root-base IMG          指定根盘基准镜像 (默认: ${BASE_ROOT_IMG_DEFAULT})
  --f2fs-base IMG          指定 f2fs 基准镜像 (默认: ${BASE_F2FS_IMG_DEFAULT})
  --instance-dir DIR       指定实例根目录 (默认: ${INSTANCE_DIR_DEFAULT})
  --share-src DIR          指定共享目录模板 (默认: ${BASE_SHARE_DIR_DEFAULT})
  --ssh-port PORT          指定宿主转发到 guest 22 的端口
  --http-port PORT         指定宿主转发到 guest 80 的端口
  --port-offset N          在默认 5022/5080 上叠加偏移量
  --qga-sock PATH          指定 QGA unix socket (默认: /tmp/qga.<实例名>.sock)
  --qmp-sock PATH          指定 QMP unix socket (默认: /tmp/qemu-qmp.<实例名>.sock)
  --log FILE               指定 guest console 日志 (默认: <实例目录>/guest_console.log)
  --pid-file FILE          指定 qemu pid 文件 (默认: <实例目录>/qemu.pid)
  --dry-run                仅打印解析后的配置和 QEMU 命令，不真正启动

cleanup 选项:
  -f, --force              不交互确认，直接清理

说明:
  1. 如果实例名以数字结尾且未显式指定端口，则默认使用:
     ssh_port  = ${DEFAULT_SSH_PORT_BASE} + (suffix - 1)
     http_port = ${DEFAULT_HTTP_PORT_BASE} + (suffix - 1)
  2. 如果实例名不带数字后缀，请显式传入 --ssh-port/--http-port 或 --port-offset。
  3. 每个实例都会写出 <实例目录>/instance.env，供自动化工具读取端口、socket、log 和 pid 信息。
EOF
}

require_instance() {
  if [[ -z "${INSTANCE_NAME}" || "${INSTANCE_NAME}" == "-h" || "${INSTANCE_NAME}" == "--help" ]]; then
    usage
    exit 2
  fi
}

extract_numeric_suffix() {
  local name="$1"
  if [[ "${name}" =~ ([0-9]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

calc_port_offset() {
  if [[ -n "${PORT_OFFSET:-}" ]]; then
    printf '%s\n' "${PORT_OFFSET}"
    return 0
  fi

  local suffix
  suffix="$(extract_numeric_suffix "${INSTANCE_NAME}")"
  if [[ -z "${suffix}" ]]; then
    echo "错误: 实例名 '${INSTANCE_NAME}' 不带数字后缀，请显式传入 --ssh-port/--http-port 或 --port-offset" >&2
    exit 1
  fi

  printf '%s\n' "$((10#${suffix} - 1))"
}

derive_default_ports() {
  local offset
  offset="$(calc_port_offset)"

  if [[ -z "${SSH_PORT:-}" ]]; then
    SSH_PORT="$((DEFAULT_SSH_PORT_BASE + offset))"
  fi
  if [[ -z "${HTTP_PORT:-}" ]]; then
    HTTP_PORT="$((DEFAULT_HTTP_PORT_BASE + offset))"
  fi
}

instance_path_for() {
  printf '%s/%s\n' "${INSTANCE_DIR}" "${INSTANCE_NAME}"
}

load_instance_paths() {
  INSTANCE_PATH="$(instance_path_for)"
  INSTANCE_ROOT_IMG="${INSTANCE_PATH}/root.qcow2"
  INSTANCE_F2FS_IMG="${INSTANCE_PATH}/f2fs.qcow2"
  INSTANCE_SHARED_DIR="${INSTANCE_PATH}/shared_with_qemu"
  INSTANCE_META_FILE="${INSTANCE_PATH}/instance.env"
  PID_FILE="${PID_FILE:-${INSTANCE_PATH}/qemu.pid}"
  LOG_FILE="${LOG_FILE:-${INSTANCE_PATH}/guest_console.log}"
  QGA_SOCK="${QGA_SOCK:-/tmp/qga.${INSTANCE_NAME}.sock}"
  QMP_SOCK="${QMP_SOCK:-/tmp/qemu-qmp.${INSTANCE_NAME}.sock}"
}

load_instance_metadata_if_present() {
  load_instance_paths
  if [[ -f "${INSTANCE_META_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${INSTANCE_META_FILE}"
    SSH_PORT="${VM_SSH_PORT:-${SSH_PORT:-}}"
    HTTP_PORT="${VM_HTTP_PORT:-${HTTP_PORT:-}}"
    QGA_SOCK="${VM_QGA_SOCK:-${QGA_SOCK}}"
    QMP_SOCK="${VM_QMP_SOCK:-${QMP_SOCK}}"
    LOG_FILE="${VM_CONSOLE_LOG:-${LOG_FILE}}"
    PID_FILE="${VM_PID_FILE:-${PID_FILE}}"
    KERDIR="${VM_KERNEL_DIR:-${KERDIR:-${DEFAULT_KERDIR}}}"
    MEM="${VM_MEM:-${MEM:-${DEFAULT_MEM}}}"
    BASE_ROOT_IMG="${VM_BASE_ROOT_IMG:-${BASE_ROOT_IMG:-${BASE_ROOT_IMG_DEFAULT}}}"
    BASE_F2FS_IMG="${VM_BASE_F2FS_IMG:-${BASE_F2FS_IMG:-${BASE_F2FS_IMG_DEFAULT}}}"
  fi
}

write_instance_metadata() {
  mkdir -p "${INSTANCE_PATH}"
  cat > "${INSTANCE_META_FILE}" <<EOF
INSTANCE_NAME=${INSTANCE_NAME}
INSTANCE_PATH=${INSTANCE_PATH}
INSTANCE_ROOT_IMG=${INSTANCE_ROOT_IMG}
INSTANCE_F2FS_IMG=${INSTANCE_F2FS_IMG}
INSTANCE_SHARED_DIR=${INSTANCE_SHARED_DIR}
VM_SSH_HOST=127.0.0.1
VM_SSH_PORT=${SSH_PORT}
VM_HTTP_PORT=${HTTP_PORT}
VM_QGA_SOCK=${QGA_SOCK}
VM_QMP_SOCK=${QMP_SOCK}
VM_CONSOLE_LOG=${LOG_FILE}
VM_PID_FILE=${PID_FILE}
VM_KERNEL_DIR=${KERDIR}
VM_MEM=${MEM}
VM_BASE_ROOT_IMG=${BASE_ROOT_IMG}
VM_BASE_F2FS_IMG=${BASE_F2FS_IMG}
EOF
}

print_instance_metadata() {
  local status="$1"
  cat <<EOF
instance_name=${INSTANCE_NAME}
instance_path=${INSTANCE_PATH}
root_overlay=${INSTANCE_ROOT_IMG}
f2fs_overlay=${INSTANCE_F2FS_IMG}
shared_dir=${INSTANCE_SHARED_DIR}
pid_file=${PID_FILE}
console_log=${LOG_FILE}
ssh_port=${SSH_PORT}
http_port=${HTTP_PORT}
qga_sock=${QGA_SOCK}
qmp_sock=${QMP_SOCK}
status=${status}
EOF
}

print_qemu_command() {
  printf 'qemu_command='
  printf '%q ' "${QEMU_CMD[@]}"
  printf '\n'
}

ensure_instance_seeded() {
  if [[ ! -f "${BASE_ROOT_IMG}" || ! -f "${BASE_F2FS_IMG}" ]]; then
    echo "错误: 找不到基准镜像 '${BASE_ROOT_IMG}' 或 '${BASE_F2FS_IMG}'" >&2
    exit 1
  fi

  mkdir -p "${INSTANCE_PATH}" "${INSTANCE_SHARED_DIR}"

  if [[ ! -f "${INSTANCE_ROOT_IMG}" ]]; then
    echo "创建 CoW 根文件系统镜像..."
    qemu-img create -f qcow2 -b "${BASE_ROOT_IMG}" -F raw "${INSTANCE_ROOT_IMG}"
  fi

  if [[ ! -f "${INSTANCE_F2FS_IMG}" ]]; then
    echo "创建 CoW f2fs 数据盘镜像..."
    qemu-img create -f qcow2 -b "${BASE_F2FS_IMG}" -F raw "${INSTANCE_F2FS_IMG}"
  fi

  if [[ -d "${SHARE_SRC}" ]] && [[ -z "$(find "${INSTANCE_SHARED_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "复制共享目录模板到实例目录..."
    cp -a "${SHARE_SRC}/." "${INSTANCE_SHARED_DIR}/"
  fi
}

build_qemu_command() {
  local kernel_append
  kernel_append='panic=5 noinitrd root=/dev/vda rw console=ttyAMA0 nokaslr loglevel=8 ramoops.mem_address=0x1FF800000 ramoops.mem_size=0x200000 ramoops.record_size=0x20000 ramoops.console_size=0x20000 panic_on_oops=1 sysrq_always_enabled'

  QEMU_CMD=(
    qemu-system-aarch64
    -smp 8
    -machine virt,virtualization=true,gic-version=3
    -nographic
    -m "size=${MEM}"
    -mem-prealloc
    -cpu cortex-a72
    -kernel "${KERDIR}/arch/arm64/boot/Image"
    -append "${kernel_append}"
    -netdev "user,id=eth0,hostfwd=tcp::${SSH_PORT}-:22,hostfwd=tcp::${HTTP_PORT}-:80"
    -device virtio-net-device,netdev=eth0
    -chardev "socket,path=${QGA_SOCK},server=on,wait=off,id=qga0"
    -device virtio-serial-pci
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0
    -drive "format=qcow2,file=${INSTANCE_ROOT_IMG},if=virtio,id=rootdisk"
    -drive "format=qcow2,file=${INSTANCE_F2FS_IMG},if=virtio,id=f2fsnorm"
    -virtfs "local,path=${INSTANCE_SHARED_DIR},mount_tag=hostshare,security_model=passthrough,id=hostshare"
    -pidfile "${PID_FILE}"
    -s
    -qmp "unix:${QMP_SOCK},server=on,wait=off"
  )
}

start_instance() {
  KERDIR="${DEFAULT_KERDIR}"
  MEM="${DEFAULT_MEM}"
  BASE_ROOT_IMG="${BASE_ROOT_IMG_DEFAULT}"
  BASE_F2FS_IMG="${BASE_F2FS_IMG_DEFAULT}"
  INSTANCE_DIR="${INSTANCE_DIR_DEFAULT}"
  SHARE_SRC="${BASE_SHARE_DIR_DEFAULT}"
  DRY_RUN=0
  PORT_OFFSET=""
  SSH_PORT=""
  HTTP_PORT=""
  QGA_SOCK=""
  QMP_SOCK=""
  LOG_FILE=""
  PID_FILE=""

  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -k|--kerdir) KERDIR="$2"; shift 2 ;;
      -m|--mem) MEM="$2"; shift 2 ;;
      --root-base) BASE_ROOT_IMG="$2"; shift 2 ;;
      --f2fs-base) BASE_F2FS_IMG="$2"; shift 2 ;;
      --instance-dir) INSTANCE_DIR="$2"; shift 2 ;;
      --share-src) SHARE_SRC="$2"; shift 2 ;;
      --ssh-port) SSH_PORT="$2"; shift 2 ;;
      --http-port) HTTP_PORT="$2"; shift 2 ;;
      --port-offset) PORT_OFFSET="$2"; shift 2 ;;
      --qga-sock) QGA_SOCK="$2"; shift 2 ;;
      --qmp-sock) QMP_SOCK="$2"; shift 2 ;;
      --log) LOG_FILE="$2"; shift 2 ;;
      --pid-file) PID_FILE="$2"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "错误: 'start' 命令的未知选项 '$1'" >&2; usage; exit 1 ;;
    esac
  done

  derive_default_ports
  load_instance_paths

  if [[ -f "${PID_FILE}" ]]; then
    local existing_pid
    existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${existing_pid}" ]] && ps -p "${existing_pid}" > /dev/null 2>&1; then
      echo "错误: 实例 '${INSTANCE_NAME}' 已在运行 (PID: ${existing_pid})" >&2
      exit 1
    fi
  fi

  mkdir -p "$(dirname "${LOG_FILE}")" "$(dirname "${PID_FILE}")"
  ensure_instance_seeded
  write_instance_metadata

  echo "================ QEMU 配置 (${INSTANCE_NAME}) ================"
  echo "Kernel Directory : ${KERDIR}"
  echo "Memory Size      : ${MEM}"
  echo "Root Overlay     : ${INSTANCE_ROOT_IMG}"
  echo "F2FS Overlay     : ${INSTANCE_F2FS_IMG}"
  echo "Shared Directory : ${INSTANCE_SHARED_DIR}"
  echo "Console Log      : ${LOG_FILE}"
  echo "PID File         : ${PID_FILE}"
  echo "SSH Port         : ${SSH_PORT}"
  echo "HTTP Port        : ${HTTP_PORT}"
  echo "QGA Socket       : ${QGA_SOCK}"
  echo "QMP Socket       : ${QMP_SOCK}"
  echo "========================================================"
  print_instance_metadata "prepared"

  rm -f "${QGA_SOCK}" "${QMP_SOCK}"
  build_qemu_command

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    print_qemu_command
    exit 0
  fi

  "${QEMU_CMD[@]}" 2>&1 | tee "${LOG_FILE}"
}

stop_instance() {
  INSTANCE_DIR="${INSTANCE_DIR_DEFAULT}"
  PID_FILE=""
  QGA_SOCK=""
  QMP_SOCK=""

  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance-dir) INSTANCE_DIR="$2"; shift 2 ;;
      --pid-file) PID_FILE="$2"; shift 2 ;;
      --qga-sock) QGA_SOCK="$2"; shift 2 ;;
      --qmp-sock) QMP_SOCK="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "错误: 'stop' 命令的未知选项 '$1'" >&2; usage; exit 1 ;;
    esac
  done

  load_instance_metadata_if_present

  if [[ ! -f "${PID_FILE}" ]]; then
    echo "错误: 找不到实例 '${INSTANCE_NAME}' 的 PID 文件 ${PID_FILE}" >&2
    exit 1
  fi

  local pid
  pid="$(cat "${PID_FILE}")"
  echo "正在停止实例 '${INSTANCE_NAME}' (PID: ${pid})..."

  if ps -p "${pid}" > /dev/null 2>&1; then
    kill "${pid}"
    while ps -p "${pid}" > /dev/null 2>&1; do
      sleep 1
    done
  else
    echo "警告: PID ${pid} 不存在，执行清理。"
  fi

  rm -f "${PID_FILE}" "${QGA_SOCK}" "${QMP_SOCK}"
  print_instance_metadata "stopped"
}

status_instance() {
  INSTANCE_DIR="${INSTANCE_DIR_DEFAULT}"
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance-dir) INSTANCE_DIR="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "错误: 'status' 命令的未知选项 '$1'" >&2; usage; exit 1 ;;
    esac
  done

  KERDIR="${DEFAULT_KERDIR}"
  MEM="${DEFAULT_MEM}"
  BASE_ROOT_IMG="${BASE_ROOT_IMG_DEFAULT}"
  BASE_F2FS_IMG="${BASE_F2FS_IMG_DEFAULT}"
  SSH_PORT=""
  HTTP_PORT=""
  QGA_SOCK=""
  QMP_SOCK=""
  LOG_FILE=""
  PID_FILE=""

  load_instance_metadata_if_present
  derive_default_ports
  load_instance_paths

  local state="missing"
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && ps -p "${pid}" > /dev/null 2>&1; then
      state="running"
    else
      state="stale-pid"
    fi
  elif [[ -d "${INSTANCE_PATH}" ]]; then
    state="prepared"
  fi

  print_instance_metadata "${state}"
}

cleanup_instance() {
  INSTANCE_DIR="${INSTANCE_DIR_DEFAULT}"
  FORCE=0

  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instance-dir) INSTANCE_DIR="$2"; shift 2 ;;
      -f|--force) FORCE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "错误: 'cleanup' 命令的未知选项 '$1'" >&2; usage; exit 1 ;;
    esac
  done

  load_instance_paths

  if [[ "${FORCE}" -ne 1 ]]; then
    echo "警告: 这将永久删除实例 '${INSTANCE_NAME}' 的所有数据。"
    read -r -p "包括: ${INSTANCE_PATH} 目录及其所有内容。确定吗? (y/N) " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
      echo "操作已取消。"
      exit 0
    fi
  fi

  if [[ -f "${INSTANCE_PATH}/qemu.pid" ]]; then
    "${BASH_SOURCE[0]}" stop "${INSTANCE_NAME}" --instance-dir "${INSTANCE_DIR}" || true
  fi

  echo "正在删除实例目录: ${INSTANCE_PATH}"
  rm -rf "${INSTANCE_PATH}"
  rm -f "/tmp/qga.${INSTANCE_NAME}.sock" "/tmp/qemu-qmp.${INSTANCE_NAME}.sock"
  echo "实例 '${INSTANCE_NAME}' 已被彻底清理。"
}

if [[ -z "${COMMAND}" || "${COMMAND}" == "-h" || "${COMMAND}" == "--help" ]]; then
  usage
  exit 0
fi

case "${COMMAND}" in
  start)
    require_instance
    start_instance "$@"
    ;;
  stop)
    require_instance
    stop_instance "$@"
    ;;
  status)
    require_instance
    status_instance "$@"
    ;;
  cleanup)
    require_instance
    cleanup_instance "$@"
    ;;
  *)
    echo "错误: 未知命令 '${COMMAND}'" >&2
    usage
    exit 1
    ;;
esac
