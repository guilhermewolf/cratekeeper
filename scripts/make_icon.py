#!/usr/bin/env python3
"""Render the app icon (1024x1024 PNG) with the standard library only.

A macOS-style rounded square holding a vinyl record with a blue label —
the dashboard's palette. Run: python scripts/make_icon.py packaging/icon.png
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 1024
SS = 2  # supersampling factor for anti-aliasing

BG_TOP = (0x24, 0x24, 0x23)
BG_BOTTOM = (0x12, 0x12, 0x12)
DISC = (0x1A, 0x1A, 0x19)
GROOVE = (0x2C, 0x2C, 0x2A)
SHEEN = (0x3A, 0x3A, 0x38)
LABEL = (0x39, 0x87, 0xE5)
LABEL_DARK = (0x2A, 0x66, 0xB5)
HOLE = (0x0D, 0x0D, 0x0D)
ACCENT = (0x19, 0x9E, 0x70)


def rounded_rect_coverage(x: float, y: float, half: float, radius: float) -> float:
    """1 inside a rounded square centred at the origin, 0 outside, soft edge."""
    qx, qy = abs(x) - (half - radius), abs(y) - (half - radius)
    dx, dy = max(qx, 0.0), max(qy, 0.0)
    dist = math.hypot(dx, dy) + min(max(qx, qy), 0.0) - radius
    return clamp(0.5 - dist)


def clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def shade(px: float, py: float) -> tuple[float, float, float, float]:
    """Colour + alpha for one (supersampled) pixel in [0, SIZE) space."""
    cx = px - SIZE / 2
    cy = py - SIZE / 2
    half = SIZE * 0.41  # Big Sur icons leave ~9% margin around the square
    cov = rounded_rect_coverage(cx, cy, half, half * 0.45)
    if cov <= 0:
        return (0, 0, 0, 0)

    t = (py / SIZE)
    color = mix(BG_TOP, BG_BOTTOM, t)

    # vinyl disc
    r = math.hypot(cx, cy)
    disc_r = half * 0.80
    if r < disc_r + 1:
        edge = clamp(disc_r - r + 0.5)
        ring = math.sin(r / 6.5) * 0.5 + 0.5  # grooves
        base = mix(DISC, GROOVE, ring * 0.9)
        # soft diagonal sheen like light on vinyl
        ang = math.atan2(cy, cx)
        sheen = max(0.0, math.cos(ang - math.radians(-50))) ** 6 * 0.55
        sheen += max(0.0, math.cos(ang - math.radians(130))) ** 6 * 0.35
        base = mix(base, SHEEN, sheen * (r / disc_r))
        color = mix(color, base, edge)

        label_r = disc_r * 0.36
        if r < label_r + 1:
            e2 = clamp(label_r - r + 0.5)
            lt = clamp((cy / label_r + 1) / 2)
            lab = mix(LABEL, LABEL_DARK, lt)
            color = mix(color, lab, e2)
            # a green "MIK confirmed" tick-ring on the label
            ring_r = label_r * 0.72
            band = clamp(6 - abs(r - ring_r))
            color = mix(color, ACCENT, band * 0.9)
            hole_r = disc_r * 0.045
            if r < hole_r + 1:
                color = mix(color, HOLE, clamp(hole_r - r + 0.5))

    return (color[0], color[1], color[2], 255 * cov)


def render(path: Path) -> None:
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    r, g, b, a = shade(x + (sx + 0.5) / SS, y + (sy + 0.5) / SS)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = SS * SS
            if acc[3] > 0:
                row += bytes((int(acc[0] / acc[3]), int(acc[1] / acc[3]),
                              int(acc[2] / acc[3]), int(acc[3] / n)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "packaging/icon.png")
    render(out)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
