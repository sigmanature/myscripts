from dataclasses import dataclass
from typing import Iterator, Tuple


@dataclass(frozen=True)
class PatternConfig:
    mode: str
    token: bytes
    seed: int
    chunk_size: int
    pattern_gen: str
    readback: str


def parse_bytes(size_str: str) -> int:
    """Parse '123', '4k', '10m', '1g', '512b' into bytes."""
    s = str(size_str).strip().lower()
    if not s:
        raise ValueError("size string cannot be empty")

    unit = s[-1]
    if unit.isalpha():
        num_part = s[:-1]
        factors = {
            "b": 1,
            "k": 1024,
            "m": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
        }
        if unit not in factors:
            raise ValueError(f"invalid unit '{unit}' in '{s}'")
        factor = factors[unit]
    else:
        num_part = s
        factor = 1

    if not num_part.isdigit():
        if len(s) == 1 and s.isalpha():
            raise ValueError(f"missing number before unit in '{s}'")
        raise ValueError(f"invalid number part '{num_part}' in '{s}'")

    return int(num_part) * factor


def _repeat_bytes(token: bytes, start_idx: int, n: int) -> bytes:
    if n <= 0:
        return b""
    if not token:
        raise ValueError("pattern token cannot be empty")

    token_len = len(token)
    start_idx %= token_len
    out = bytearray(n)

    first = min(n, token_len - start_idx)
    out[:first] = token[start_idx:start_idx + first]
    filled = first
    while filled < n:
        take = min(token_len, n - filled)
        out[filled:filled + take] = token[:take]
        filled += take
    return bytes(out)


def render_pattern_bytes(abs_pos: int, n: int, config: PatternConfig, overlay_off: int = 0) -> bytes:
    if config.mode == "counter":
        base = config.seed + abs_pos
        return bytes(((base + i) & 0xFF) for i in range(n))
    if config.mode == "mod251":
        base = config.seed + abs_pos
        return bytes(((base + i) % 251) for i in range(n))
    if config.mode == "filepos":
        return _repeat_bytes(config.token, abs_pos, n)
    if config.mode == "repeat":
        return _repeat_bytes(config.token, abs_pos - overlay_off, n)
    raise ValueError(f"unknown pattern mode: {config.mode}")


def iter_expected_chunks(
    offset: int,
    size: int,
    config: PatternConfig,
) -> Iterator[Tuple[int, bytes]]:
    """Yield (relative_pos, expected_chunk) for [offset, offset+size)."""
    pos = 0
    while pos < size:
        chunk = min(config.chunk_size, size - pos)
        yield pos, render_pattern_bytes(offset + pos, chunk, config)
        pos += chunk


def generate_expected_direct(offset: int, size: int, config: PatternConfig) -> bytes:
    parts = []
    for _, chunk in iter_expected_chunks(offset, size, config):
        parts.append(chunk)
    return b"".join(parts)
