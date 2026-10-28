"""Draw the app icon and render the PNGs the page links to (needs rsvg-convert: brew install librsvg).

    python3 scripts/make_icons.py

Writes apps_dashboard/static/icon.svg (the source), apple-touch-icon.png (180 px, iOS
"Add to Home Screen"), icon-192.png, icon-512.png (manifest), favicon.svg and
favicon-32.png.  The mark is a 2 x 2 grid of app tiles, one per status colour.
The square is full-bleed: iOS and Android round the corners.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "apps_dashboard" / "static"
TILES = (("#51cf66", 0, 0), ("#4dabf7", 1, 0), ("#ffa94d", 0, 1), ("#e9ecef", 1, 1))


def svg(size=1024, pad=0.2, gap=0.07, radius=0.09, background=True):
    inner = size * (1 - 2 * pad)
    tile = (inner - size * gap) / 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">']
    if background:
        parts.append(f'<rect width="{size}" height="{size}" fill="#1b1f24"/>')
    for colour, col, row in TILES:
        x = size * pad + col * (tile + size * gap)
        y = size * pad + row * (tile + size * gap)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{tile:.1f}" height="{tile:.1f}" '
                     f'rx="{size * radius:.1f}" fill="{colour}"/>')
    # A status dot on the last tile: the dashboard's one idea.
    cx = size * pad + tile + size * gap + tile / 2
    cy = size * pad + tile + size * gap + tile / 2
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{tile * 0.22:.1f}" fill="#2f9e44"/>')
    parts.append("</svg>\n")
    return "".join(parts)


def render(source, out, px):
    subprocess.run(["rsvg-convert", "-w", str(px), "-h", str(px), "-o", str(out), str(source)], check=True)


def main():
    if not shutil.which("rsvg-convert"):
        sys.exit("needs rsvg-convert (brew install librsvg)")
    STATIC.mkdir(parents=True, exist_ok=True)
    (STATIC / "icon.svg").write_text(svg(), encoding="utf-8")
    favicon = svg(pad=0.04, gap=0.1, radius=0.12, background=False)
    (STATIC / "favicon.svg").write_text(favicon, encoding="utf-8")
    for name, px in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        render(STATIC / "icon.svg", STATIC / name, px)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "favicon.svg"
        src.write_text(favicon, encoding="utf-8")
        render(src, STATIC / "favicon-32.png", 32)
    print(f"wrote icons in {STATIC}")


if __name__ == "__main__":
    main()
