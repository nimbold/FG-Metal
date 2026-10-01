"""Small strict P6 PPM reader/writer shared by the RIFE fixture tools."""

from __future__ import annotations

from pathlib import Path


def _token(data: bytes, offset: int) -> tuple[bytes, int]:
    length = len(data)
    while offset < length:
        byte = data[offset]
        if byte in b" \t\r\n\v\f":
            offset += 1
            continue
        if byte == ord("#"):
            newline = data.find(b"\n", offset + 1)
            if newline < 0:
                raise ValueError("unterminated comment in PPM header")
            offset = newline + 1
            continue
        break

    start = offset
    while offset < length and data[offset] not in b" \t\r\n\v\f#":
        offset += 1
    if start == offset:
        raise ValueError("truncated PPM header")
    return data[start:offset], offset


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    """Read an 8-bit binary RGB P6 PPM, rejecting truncated or trailing data."""
    data = path.read_bytes()
    tokens: list[bytes] = []
    offset = 0
    for _ in range(4):
        value, offset = _token(data, offset)
        tokens.append(value)

    if tokens[0] != b"P6":
        raise ValueError(f"{path}: expected binary P6 PPM")
    try:
        width = int(tokens[1])
        height = int(tokens[2])
        maxval = int(tokens[3])
    except ValueError as exc:
        raise ValueError(f"{path}: invalid PPM dimensions or maxval") from exc
    if width <= 0 or height <= 0 or maxval != 255:
        raise ValueError(f"{path}: expected positive dimensions and maxval 255")

    # The P6 raster begins after one whitespace separator. Treat CRLF as one
    # line-ending separator, but do not skip further bytes because a pixel may
    # itself begin with a whitespace-valued channel.
    if offset >= len(data) or data[offset] not in b" \t\r\n\v\f":
        raise ValueError(f"{path}: missing PPM raster separator")
    if data[offset:offset + 2] == b"\r\n":
        offset += 2
    else:
        offset += 1

    expected_bytes = width * height * 3
    raster = data[offset:]
    if len(raster) != expected_bytes:
        raise ValueError(
            f"{path}: expected {expected_bytes} raster bytes for {width}x{height}, got {len(raster)}"
        )
    return width, height, raster


def write_ppm(path: Path, width: int, height: int, rgb: bytes) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("PPM width and height must be positive")
    expected_bytes = width * height * 3
    if len(rgb) != expected_bytes:
        raise ValueError(f"expected {expected_bytes} RGB bytes, got {len(rgb)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)
