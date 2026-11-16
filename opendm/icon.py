"""Procedurally drawn app icon: a blue globe with a green download badge.

Kept in code so the repo needs no binary assets; ``python -m opendm.icon`` writes the
PNGs used by the browser extension.
"""

import base64
import math
import os
import struct
import zlib

# Globe
GX, GY, GR = 0.44, 0.44, 0.41
# Download badge (bottom right), with a transparent gap around it
BX, BY, BR, GAP = 0.735, 0.735, 0.255, 0.055

WHITE = (255, 255, 255)
GREEN = (34, 197, 94)
GRID = (191, 219, 254)


def _globe(x: float, y: float, lw: float, small: bool = False):
    dx, dy = x - GX, y - GY
    d = math.hypot(dx, dy)
    if d > GR:
        return None
    # Sea: diagonal gradient, lighter top-left
    t = max(0.0, min(1.0, (dx + dy) / (2 * GR) + 0.5))
    sea = (int(96 - 60 * t), int(165 - 75 * t), int(250 - 30 * t))
    if d > GR - lw * 1.1:  # rim
        return GRID
    ny = dy / GR
    # Parallels: equator and two latitudes
    for lat in ((0.0,) if small else (0.0, -0.5, 0.5)):
        if abs(ny - lat) * GR <= lw / 2:
            return GRID
    # Meridians: centre line and two ellipses
    half = GR * math.sqrt(max(0.0, 1 - ny * ny))
    for k in ((0.5,) if small else (0.0, 0.55)):
        if abs(abs(dx) - k * half) <= lw / 2:
            return GRID
    return sea


def _badge(x: float, y: float, lw: float):
    dx, dy = x - BX, y - BY
    d = math.hypot(dx, dy)
    if d > BR:
        return None
    u, v = dx / BR, dy / BR  # -1..1 inside the badge
    w = max(0.15, lw * 2.2 / BR)  # shaft width scales up for tiny sizes
    if abs(u) <= w and -0.62 <= v <= 0.1:
        return WHITE
    if -0.05 <= v <= 0.58 and abs(u) <= (0.58 - v) * 1.05:  # arrow head
        return WHITE
    return GREEN


def _inside_icon(x: float, y: float, size: int = 128):
    """Return (r, g, b) for a point in unit coordinates (0..1), or None if transparent."""
    small = size < 24  # tiny icons get fewer grid lines so they don't turn to mush
    lw = 0.9 / size if small else max(0.035, 1.15 / size)
    b = _badge(x, y, lw)
    if b:
        return b
    if math.hypot(x - BX, y - BY) <= BR + GAP:
        return None
    return _globe(x, y, lw, small)


def render(size: int, ss: int = 4):
    """RGBA rows, supersampled for smooth edges."""
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = _inside_icon((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size, size)
                    if c:
                        acc[0] += c[0]
                        acc[1] += c[1]
                        acc[2] += c[2]
                        acc[3] += 255
            n = ss * ss
            covered = acc[3] / 255
            if covered:
                row += bytes((int(acc[0] / covered), int(acc[1] / covered), int(acc[2] / covered), int(acc[3] / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def png_bytes(size: int) -> bytes:
    raw = b"".join(b"\0" + row for row in render(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def png_base64(size: int) -> str:
    return base64.b64encode(png_bytes(size)).decode()


def ico_bytes(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    """A Windows .ico holding one PNG per size (supported since Windows Vista)."""
    images = [png_bytes(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        dim = 0 if s >= 256 else s  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


# Drawing the icon pixel by pixel takes about half a second, so built copies of OpenDM ship
# the finished files (see tools/build_exe.py) and only a source checkout draws them.
TRAY_SIZES = (16, 20, 24, 32, 48, 64)


def _shipped(name: str) -> bytes | None:
    import sys

    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    try:
        with open(os.path.join(base, "assets", name), "rb") as f:
            return f.read()
    except OSError:
        return None


def window_png() -> str:
    """64 px PNG (base64) for the window icon."""
    data = _shipped("window64.png")
    return base64.b64encode(data).decode() if data else png_base64(64)


def tray_ico() -> bytes:
    """Multi-size .ico for the tray icon."""
    return _shipped("tray.ico") or ico_bytes(TRAY_SIZES)


def write_assets(folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "window64.png"), "wb") as f:
        f.write(png_bytes(64))
    with open(os.path.join(folder, "tray.ico"), "wb") as f:
        f.write(ico_bytes(TRAY_SIZES))


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "..", "extension", "icons")
    os.makedirs(out, exist_ok=True)
    for s in (16, 32, 48, 128):
        with open(os.path.join(out, f"icon{s}.png"), "wb") as f:
            f.write(png_bytes(s))
    print("wrote icons to", os.path.abspath(out))
