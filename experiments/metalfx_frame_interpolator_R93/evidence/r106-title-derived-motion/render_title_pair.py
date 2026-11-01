from pathlib import Path
import binascii
import struct
import sys
import zlib


def png_chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload +
            struct.pack(">I", binascii.crc32(kind + payload) & 0xffffffff))


def write_png(path, width, height, rgb):
    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                    for y in range(height))
    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) +
               png_chunk(b"IDAT", zlib.compress(rows, 6)) +
               png_chunk(b"IEND", b""))
    Path(path).write_bytes(payload)


def source_rgb(path, width, height):
    bgra = Path(path).read_bytes()
    if len(bgra) != width * height * 4:
        raise ValueError(f"{path}: expected {width * height * 4} bytes, got {len(bgra)}")
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        s, d = i * 4, i * 3
        rgb[d:d + 3] = bytes((bgra[s + 2], bgra[s + 1], bgra[s]))
    return bytes(rgb)


def midpoint_rgb(path, width, height):
    half = Path(path).read_bytes()
    if len(half) != width * height * 8:
        raise ValueError(f"{path}: expected {width * height * 8} bytes, got {len(half)}")
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        r, g, b = struct.unpack_from("<3e", half, i * 8)
        d = i * 3
        rgb[d] = round(max(0.0, min(1.0, r)) * 255)
        rgb[d + 1] = round(max(0.0, min(1.0, g)) * 255)
        rgb[d + 2] = round(max(0.0, min(1.0, b)) * 255)
    return bytes(rgb)


def main():
    if len(sys.argv) != 8:
        raise SystemExit("usage: render_title_pair.py PREVIOUS.raw CURRENT.raw M.rgba16f WIDTH HEIGHT OUT_DIR PREFIX")
    previous_path, current_path, output_path = sys.argv[1:4]
    width, height = int(sys.argv[4]), int(sys.argv[5])
    out_dir, prefix = Path(sys.argv[6]), sys.argv[7]
    previous = source_rgb(previous_path, width, height)
    current = source_rgb(current_path, width, height)
    midpoint = midpoint_rgb(output_path, width, height)
    panels = (("previous", previous), ("midpoint", midpoint), ("current", current))
    for label, pixels in panels:
        write_png(out_dir / f"{prefix}-{label}.png", width, height, pixels)
    composite = bytearray(width * 3 * height * 3)
    for y in range(height):
        dst = y * width * 3 * 3
        src = y * width * 3
        for _, pixels in panels:
            composite[dst:dst + width * 3] = pixels[src:src + width * 3]
            dst += width * 3
    write_png(out_dir / f"{prefix}-previous-M-current.png", width * 3,
              height, bytes(composite))
    print(f"TITLE_VISUALS_WRITTEN dimensions={width}x{height} files=4 prefix={prefix}")


if __name__ == "__main__":
    main()
