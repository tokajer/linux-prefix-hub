#!/usr/bin/env python3
"""Render the app icon as a PNG -- no image library required.

The build needs exactly one PNG in the AppDir, and pulling Pillow into the
build just to draw a folder with an arrow would be silly. This draws it with
signed distance fields and writes the PNG with zlib, which is stdlib.

Usage:  python3 packaging/make-icon.py [out.png] [size]
"""
from __future__ import annotations

import struct
import sys
import zlib

SS = 3  # supersampling factor -> anti-aliasing

BG_TOP = (36, 46, 84)
BG_BOTTOM = (86, 52, 128)
FOLDER = (255, 255, 255)
ARROW = (126, 231, 195)


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=False))


def _rounded_rect(x, y, w, h, r):
    """Signed distance to a rounded rectangle centred at (0,0) coords."""
    def sdf(px, py):
        dx = abs(px - x) - w / 2 + r
        dy = abs(py - y) - h / 2 + r
        outside = (max(dx, 0.0) ** 2 + max(dy, 0.0) ** 2) ** 0.5
        return outside + min(max(dx, dy), 0.0) - r
    return sdf


def _in_folder(px, py, size):
    """Folder body plus its tab, both with rounded corners."""
    body = _rounded_rect(size * 0.50, size * 0.585,
                         size * 0.52, size * 0.34, size * 0.045)
    tab = _rounded_rect(size * 0.355, size * 0.395,
                        size * 0.23, size * 0.10, size * 0.035)
    return min(body(px, py), tab(px, py))


def _in_arrow(px, py, size):
    """Downward arrow: stem plus head, pointing into the folder."""
    stem = _rounded_rect(size * 0.50, size * 0.315,
                         size * 0.085, size * 0.24, size * 0.04)
    # Triangle head: three half-planes, apex pointing down.
    x, y = px - size * 0.50, py - size * 0.47
    half, height = size * 0.13, size * 0.14
    edge = (height ** 2 + half ** 2) ** 0.5
    d_base = -(y + height / 2)
    d_left = (-height * (x + half) + half * (y + height / 2)) / edge
    d_right = (height * (x - half) + half * (y + height / 2)) / edge
    head = max(d_base, d_left, d_right)
    return min(stem(px, py), head)


def render(size: int) -> bytes:
    rows = []
    step = 1.0 / SS
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    fx = px + (sx + 0.5) * step
                    fy = py + (sy + 0.5) * step
                    colour, alpha = _sample(fx, fy, size)
                    r += colour[0] * alpha
                    g += colour[1] * alpha
                    b += colour[2] * alpha
                    a += alpha
            n = SS * SS
            if a > 0:
                row += bytes((round(r / a), round(g / a), round(b / a),
                              round(255 * a / n)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return _png(size, size, rows)


def _sample(fx, fy, size):
    """Colour + coverage of one sample point."""
    bg = _rounded_rect(size * 0.5, size * 0.5, size * 0.94, size * 0.94,
                       size * 0.22)(fx, fy)
    if bg > 0:
        return (0, 0, 0), 0.0
    colour = _mix(BG_TOP, BG_BOTTOM, fy / size)
    if _in_folder(fx, fy, size) <= 0:
        colour = FOLDER
    if _in_arrow(fx, fy, size) <= 0:
        colour = ARROW
    return colour, 1.0


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR",
                     struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "linux-prefix-hub.png"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    with open(out, "wb") as handle:
        handle.write(render(size))
    print(f"wrote {out} ({size}x{size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
