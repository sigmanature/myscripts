import os
import subprocess
from typing import Optional


def _run(argv: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, capture_output=capture, text=True)


def _need_cmd(name: str) -> None:
    if subprocess.run(["bash", "-lc", f"command -v {name}"], check=False).returncode != 0:
        raise RuntimeError(f"missing command: {name}")


def _findmnt_opts(target: str) -> str:
    cp = _run(["findmnt", "-no", "OPTIONS", "--target", target], check=False, capture=True)
    return (cp.stdout or "").strip()


def ensure_mount_has_option(mount_root: str, option: str) -> None:
    opts = _findmnt_opts(mount_root)
    if not opts:
        raise RuntimeError(f"findmnt returned empty OPTIONS for {mount_root}")
    if f",{option}," not in f",{opts},":
        raise RuntimeError(f"{mount_root} is not mounted with {option}; current options: {opts}")


def _fscrypt_status_text(path: str) -> str:
    cp = _run(["fscrypt", "status", path], check=False, capture=True)
    return ((cp.stdout or "") + (cp.stderr or "")).strip()


def _has_fscrypt_E_attr(path: str) -> bool:
    cp = _run(["lsattr", "-d", path], check=False, capture=True)
    out = (cp.stdout or "").strip().split()
    if not out:
        return False
    return "E" in out[0]


def ensure_inline_fscrypt_dir(
    *,
    mount_root: str,
    enc_dir_name: str,
    key_path: str,
    protector_name: str,
) -> str:
    """Ensure mount_root has inlinecrypt and enc_dir is fscrypt-encrypted and unlocked."""
    _need_cmd("findmnt")
    _need_cmd("fscrypt")
    _need_cmd("lsattr")

    if not os.path.isfile(key_path):
        raise RuntimeError(f"fscrypt key not found: {key_path}")

    ensure_mount_has_option(mount_root, "inlinecrypt")

    _run(["fscrypt", "setup", mount_root, "--quiet"], check=False, capture=False)

    enc_dir = os.path.join(mount_root, enc_dir_name)
    os.makedirs(enc_dir, exist_ok=True)

    status = _fscrypt_status_text(enc_dir)
    if "encrypted with fscrypt" not in status:
        if os.listdir(enc_dir):
            raise RuntimeError(f"enc dir exists but is plain/non-empty: {enc_dir}")
        _run(
            [
                "fscrypt",
                "encrypt",
                enc_dir,
                "--source=raw_key",
                "--name",
                protector_name,
                "--key",
                key_path,
                "--quiet",
            ],
            check=True,
            capture=False,
        )
        status = _fscrypt_status_text(enc_dir)

    if "encrypted with fscrypt" not in status:
        raise RuntimeError(f"failed to encrypt dir: {enc_dir}\n{status}")

    _run(["fscrypt", "unlock", enc_dir, "--key", key_path], check=False, capture=False)

    if not _has_fscrypt_E_attr(enc_dir):
        raise RuntimeError(f"enc dir missing fscrypt E attribute: {enc_dir}")

    return enc_dir

