from __future__ import annotations

import json
import math
import re
import struct
import zlib

TOKENS = ("ground", "ink", "act")
ICON_SIZES = (180, 192, 512)


def tokens(css: bytes) -> dict[str, str]:
    found = {}
    for name in TOKENS:
        hit = re.search(rb"--" + name.encode() + rb"\s*:\s*(#[0-9a-fA-F]{6})\b", css)
        if not hit:
            raise RuntimeError(f"pattern.css lost the --{name} colour, so the app files could not use it.")
        found[name] = hit[1].decode()
    return found


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def clamp(n: int, low: int, high: int) -> int:
    return min(max(n, low), high)


def row(size: int, v: float, back: bytes, front: bytes) -> bytes:
    if not 0.26 <= v <= 0.74:
        return back * size
    inner, outer = (v - 0.42) * 0.55, (v - 0.26) * 0.55
    left = clamp(math.ceil((0.5 - outer) * size), 0, size)
    right = clamp(math.floor((0.5 + outer) * size) + 1, left, size)
    if inner < 0:
        return back * left + front * (right - left) + back * (size - right)
    inner_left = clamp(math.ceil((0.5 - inner) * size), left, right)
    inner_right = clamp(math.floor((0.5 + inner) * size) + 1, inner_left, right)
    return (back * left + front * (inner_left - left) + back * (inner_right - inner_left)
            + front * (right - inner_right) + back * (size - right))


def icon(size: int, ground: str, act: str) -> bytes:
    back, front = bytes.fromhex(ground[1:]), bytes.fromhex(act[1:])
    raw = b"".join(b"\0" + row(size, y / size, back, front) for y in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def icons(colors: dict[str, str]) -> dict[str, bytes]:
    return {f"/icon-{size}.png": icon(size, colors["ground"], colors["act"]) for size in ICON_SIZES}


def manifest(body: bytes, colors: dict[str, str]) -> bytes:
    data = json.loads(body)
    data["background_color"] = data["theme_color"] = colors["ground"]
    return json.dumps(data, indent=2).encode() + b"\n"
