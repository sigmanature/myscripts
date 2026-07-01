import ctypes
import errno
import os
from dataclasses import dataclass

from .patterns import PatternConfig, render_pattern_bytes

PROT_NONE = 0
PROT_READ = 1
PROT_WRITE = 2

MAP_PRIVATE = 0x02
MAP_ANONYMOUS = 0x20


class _IOVec(ctypes.Structure):
    _fields_ = [
        ("iov_base", ctypes.c_void_p),
        ("iov_len", ctypes.c_size_t),
    ]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.mmap.argtypes = [
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_longlong,
]
_libc.mmap.restype = ctypes.c_void_p
_libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
_libc.mprotect.restype = ctypes.c_int
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.munmap.restype = ctypes.c_int
_libc.pwritev.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(_IOVec),
    ctypes.c_int,
    ctypes.c_longlong,
]
_libc.pwritev.restype = ctypes.c_ssize_t


@dataclass(frozen=True)
class ShortWriteRequest:
    file_offset: int
    user_shift: int
    invalid_tail: int
    config: PatternConfig


@dataclass(frozen=True)
class ShortWriteResult:
    file_offset: int
    requested: int
    expected_prefix: int
    written: int
    errno_value: int
    user_shift: int
    invalid_tail: int

    @property
    def short_write(self) -> bool:
        return 0 < self.written < self.requested


def _page_size() -> int:
    return os.sysconf("SC_PAGESIZE")


def _checked_ret(rc: int, name: str) -> None:
    if rc == 0:
        return
    err = ctypes.get_errno()
    raise OSError(err, f"{name} failed: errno={err}")


def issue_faulting_pwritev(fd: int, req: ShortWriteRequest) -> ShortWriteResult:
    page_size = _page_size()
    if req.file_offset < 0:
        raise ValueError("file_offset must be >= 0")
    if req.user_shift < 0 or req.user_shift >= page_size:
        raise ValueError(f"user_shift must be in [0, {page_size})")
    if req.invalid_tail <= 0:
        raise ValueError("invalid_tail must be > 0")

    valid_prefix = page_size - req.user_shift
    total_len = valid_prefix + req.invalid_tail
    map_len = 2 * page_size
    addr = _libc.mmap(
        None,
        map_len,
        PROT_READ | PROT_WRITE,
        MAP_PRIVATE | MAP_ANONYMOUS,
        -1,
        0,
    )
    if ctypes.c_void_p(addr).value in (None, ctypes.c_void_p(-1).value):
        err = ctypes.get_errno()
        raise OSError(err, f"mmap failed: errno={err}")

    try:
        pattern = render_pattern_bytes(req.file_offset, valid_prefix, req.config)
        start_addr = ctypes.c_void_p(addr + req.user_shift)
        ctypes.memmove(start_addr, pattern, valid_prefix)

        _checked_ret(
            _libc.mprotect(ctypes.c_void_p(addr + page_size), page_size, PROT_NONE),
            "mprotect",
        )

        iov = _IOVec()
        iov.iov_base = addr + req.user_shift
        iov.iov_len = total_len

        ctypes.set_errno(0)
        ret = _libc.pwritev(fd, ctypes.byref(iov), 1, req.file_offset)
        err = ctypes.get_errno()
        written = int(ret) if ret >= 0 else 0
        if ret < 0 and err != errno.EFAULT:
            raise OSError(err, f"pwritev failed: errno={err}")

        return ShortWriteResult(
            file_offset=req.file_offset,
            requested=total_len,
            expected_prefix=valid_prefix,
            written=written,
            errno_value=err,
            user_shift=req.user_shift,
            invalid_tail=req.invalid_tail,
        )
    finally:
        _checked_ret(_libc.munmap(ctypes.c_void_p(addr), map_len), "munmap")
