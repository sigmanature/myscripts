#!/usr/bin/env bash
set -euo pipefail

LOG=/tmp/xfstests_install.log
LOCK=/tmp/xfstests_install.lock

# Ensure single instance
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "install_xfstests: already running (lock=$LOCK)"
  exit 0
fi

exec > >(tee -a "$LOG") 2>&1

echo "== xfstests install start (v4) =="
date
uname -a

# Avoid "getcwd: cannot access parent directories" if caller cwd was deleted.
cd /root

git_configure_noninteractive() {
  # Avoid hanging on credential prompts
  export GIT_TERMINAL_PROMPT=0
  export GIT_ASKPASS=true
}

wait_apt() {
  echo "== waiting for any running apt/dpkg to finish =="
  while true; do
    if ps -eo comm | grep -qE '^(apt-get|dpkg)$'; then
      ps -eo pid,etime,stat,cmd | grep -E '(apt-get|dpkg)' | grep -v grep || true
      sleep 5
      continue
    fi
    break
  done
}

ensure_xfs_dev_headers() {
  echo "== ensure clean distro XFS headers =="

  # Previous attempts may have overlaid incompatible upstream headers into
  # /usr/include/xfs. Reset to package-provided headers to avoid type
  # conflicts during xfstests build (e.g. fsxattr/mount_attr issues).
  rm -f /usr/include/xfs/*.h || true
  DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y xfslibs-dev

  local hdr_probe_rc=0
  printf '#include <xfs/xfs.h>\n#include <xfs/xqm.h>\n#include <xfs/handle.h>\n' \
    | gcc -x c - -c -o /tmp/_xfs_hdr_test.o >/dev/null 2>&1 || hdr_probe_rc=$?

  if [ $hdr_probe_rc -eq 0 ]; then
    echo "distro XFS headers OK"
    return 0
  fi

  echo "FATAL: distro XFS headers still unusable (rc=$hdr_probe_rc)" >&2
  ls -la /usr/include/xfs 2>/dev/null || true
  return 1
}

wait_apt

echo "== dpkg repair =="
dpkg --configure -a || true
apt-get -f install -y || true

wait_apt

echo "== apt update =="
apt-get update -y

wait_apt

echo "== install deps =="
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git ca-certificates \
  build-essential \
  make autoconf automake libtool pkg-config gettext bison flex \
  uuid-dev libaio-dev libattr1-dev libacl1-dev libgdbm-dev libdb-dev libtirpc-dev libblkid-dev libreadline-dev \
  xfsprogs quota attr acl \
  xfslibs-dev

wait_apt

ensure_xfs_dev_headers

wait_apt

echo "== clone/update xfstests-dev =="
git_configure_noninteractive

cd /root

# If a previous clone is half-baked (no commits), wipe it and re-clone.
if [ -d /root/xfstests-dev/.git ] && ! git -C /root/xfstests-dev rev-parse HEAD >/dev/null 2>&1; then
  echo "detected incomplete repo (no HEAD yet), removing /root/xfstests-dev"
  rm -rf /root/xfstests-dev
fi

if [ -d /root/xfstests-dev/.git ]; then
  echo "xfstests-dev already present; skipping fetch"
  git -C /root/xfstests-dev status -sb || true
else
  # Prefer upstream over HTTPS; fallback to GitHub mirror if it fails.
  if ! git clone --depth 1 https://git.kernel.org/pub/scm/fs/xfs/xfstests-dev.git /root/xfstests-dev; then
    git clone --depth 1 https://github.com/kdave/xfstests.git /root/xfstests-dev
  fi
fi

echo "== build/install xfstests-dev =="
cd /root/xfstests-dev

# Some trees probe struct mount_attr during configure but forget to include
# linux/mount.h in vfs/missing.h, which breaks src/feature build.
if ! grep -q '^#include <linux/mount.h>$' src/vfs/missing.h; then
  sed -i '/^#include <linux\/types.h>$/a #include <linux/mount.h>' src/vfs/missing.h
fi

if [ ! -x ./configure ]; then
  if [ -x ./autogen.sh ]; then
    ./autogen.sh
  else
    autoreconf -fi
  fi
fi
./configure --libexecdir=/usr/lib --exec_prefix=/var/lib

make -j"$(nproc)"
# Some trees keep install-sh only under include/, while install rules in
# ltp/src invoke ../install-sh from subdirs.
if [ ! -x ./install-sh ] && [ -x ./include/install-sh ]; then
  cp ./include/install-sh ./install-sh
  chmod +x ./install-sh
fi
make install

echo "== verify =="
command -v check || true
ls -l /usr/local/bin/check || true
check -h 2>/dev/null || true

echo "== xfstests install done =="
date
