import os
import shutil
import subprocess
from typing import Optional


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
SHARED_ROOT = os.path.dirname(TEST_ROOT)


def _run(argv: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, capture_output=capture, text=True)


def find_f2fs_io(explicit_path: Optional[str] = None) -> str:
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.extend(
        [
            os.path.join(SHARED_ROOT, "f2fs-tools", "f2fs-tools", "tools", "f2fs_io", "f2fs_io"),
            os.path.join(SHARED_ROOT, "f2fs-tools", "tools", "f2fs_io", "f2fs_io"),
        ]
    )

    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    fallback = shutil.which("f2fs_io")
    if fallback:
        return fallback
    raise RuntimeError("missing f2fs_io binary")


def has_fsverity_flag(path: str) -> bool:
    cp = _run(["lsattr", "-d", path], check=False, capture=True)
    if cp.returncode != 0:
        raise RuntimeError(f"lsattr failed for {path}: {(cp.stderr or cp.stdout).strip()}")
    parts = (cp.stdout or "").strip().split()
    if not parts:
        return False
    return "V" in parts[0]


def enable_fsverity(path: str, *, tool_path: Optional[str] = None) -> None:
    tool = find_f2fs_io(tool_path)
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o222:
        os.chmod(path, mode & ~0o222)

    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    cp = _run([tool, "set_verity", path], check=False, capture=True)
    if cp.returncode != 0:
        stderr = (cp.stderr or "").strip()
        stdout = (cp.stdout or "").strip()
        raise RuntimeError(f"set_verity failed for {path}: {stderr or stdout or cp.returncode}")

    if not has_fsverity_flag(path):
        raise RuntimeError(f"fs-verity flag missing after enable: {path}")
