#!/usr/bin/env python3
"""把一張方形 icon 圖轉成 macOS app 需要的全套資產。

    python3 tools/make-icon.py <來源.png> <輸出目錄> [名稱]

生成的圖通常是「圓角 icon 畫在不透明方形上」：四角是白的而不是透明的。
直接拿去當 .icns，每個圓角後面都會有一塊白。這裡會裁掉外圍背景、套上 Apple 的
squircle 遮罩讓四角真的透明、內縮到 macOS 的比例，再輸出全尺寸與 iconset。

刻意只用 Pillow 與標準庫：icon 資產不該因為 Xcode 工具鏈壞掉就做不出來。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

#: Apple 的 app icon 並非滿版：1024 的畫布裡，形狀大約佔 824。
CONTENT_RATIO = 824 / 1024
#: 超橢圓次方。單純的圓角矩形擺在真正的 macOS icon 旁邊會看得出不對，
#: 連續曲率才是它們看起來「坐得住」的原因。
SQUIRCLE_EXPONENT = 5.0
SIZES = [16, 32, 64, 128, 256, 512, 1024]
#: 遮罩先在放大的畫布上畫再縮小，邊緣才不會有鋸齒。
SUPERSAMPLE = 4


#: 生成的圖常在圓角形狀外圈畫一道深色描邊。遮罩會沿著自己的曲線切，
#: 描邊就留在邊緣變成一圈黑框，所以裁切時往內收一點把它切掉。
EDGE_INSET = 0.042


def trim_background(image: Image.Image, tolerance: int = 26) -> Image.Image:
    """裁掉四周與角落同色的邊，收掉描邊，並切成正方形。

    取角落的像素當背景色，所以白邊、透明邊、或帶色的邊都適用。
    """

    rgba = image.convert("RGBA")
    corner = rgba.getpixel((1, 1))
    if corner[3] < 12:
        bbox = rgba.getbbox()
        return _square_inset(rgba, bbox) if bbox else rgba

    background = Image.new("RGBA", rgba.size, corner)
    from PIL import ImageChops

    diff = ImageChops.difference(rgba.convert("RGB"), background.convert("RGB"))
    mask = diff.convert("L").point(lambda value: 255 if value > tolerance else 0)
    bbox = mask.getbbox()
    if not bbox:
        return rgba
    return _square_inset(rgba, bbox)


def _square_inset(image: Image.Image, bbox: tuple[int, int, int, int]) -> Image.Image:
    """Centre the artwork in a square crop, pulled in past its own outline."""

    left, top, right, bottom = bbox
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    side = max(right - left, bottom - top)
    side -= side * EDGE_INSET * 2
    half = side / 2
    box = (
        max(0, round(cx - half)),
        max(0, round(cy - half)),
        min(image.width, round(cx + half)),
        min(image.height, round(cy + half)),
    )
    return image.crop(box)


def squircle_mask(size: int, exponent: float = SQUIRCLE_EXPONENT) -> Image.Image:
    """Apple 的連續圓角，用超橢圓畫，不是圓角矩形。"""

    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    radius = big / 2
    points = []
    steps = 1440
    for step in range(steps + 1):
        theta = step / steps * 2 * 3.141592653589793
        import math

        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        x = radius + radius * abs(cos_t) ** (2 / exponent) * (1 if cos_t >= 0 else -1)
        y = radius + radius * abs(sin_t) ** (2 / exponent) * (1 if sin_t >= 0 else -1)
        points.append((x, y))
    draw.polygon(points, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def render(artwork: Image.Image, size: int) -> Image.Image:
    content = round(size * CONTENT_RATIO)
    offset = (size - content) // 2
    shaped = artwork.resize((content, content), Image.LANCZOS)
    shaped.putalpha(squircle_mask(content))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(shaped, (offset, offset), shaped)
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("name", nargs="?", default="AppIcon")
    parser.add_argument("--no-icns", action="store_true")
    args = parser.parse_args()

    artwork = trim_background(Image.open(args.source))
    print(f"裁出 {artwork.width}x{artwork.height}")
    args.outdir.mkdir(parents=True, exist_ok=True)
    iconset = args.outdir / f"{args.name}.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    for size in SIZES:
        image = render(artwork, size)
        image.save(args.outdir / f"{args.name}-{size}.png")
        if size <= 512:
            image.save(iconset / f"icon_{size}x{size}.png")
        if size >= 32:
            image.save(iconset / f"icon_{size // 2}x{size // 2}@2x.png")
    print(f"iconset：{iconset}")

    if not args.no_icns:
        icns = args.outdir / f"{args.name}.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True
        )
        print(f"icns：{icns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
