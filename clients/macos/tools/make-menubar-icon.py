#!/usr/bin/env python3
"""畫選單列用的 template 圖示。

    python3 tools/make-menubar-icon.py <輸出目錄>

選單列圖示是 template image：只有黑色與 alpha，由系統依深淺色自動染色。
不能直接把 app icon 縮小——那是實心插畫，縮到 18pt 只會變成一團。
這裡重畫一個同語言的線稿版：一樣是斜視的杯子加三根音量條，但只留在 18pt
還活得下來的結構。

狀態各有一張：閒置（空杯）、聆聽中（音量條變高）、錯誤（杯子打叉）。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

#: 先在放大的畫布上畫再縮小，邊緣才不會鋸齒。
SUPERSAMPLE = 8
#: 設計格線；所有座標都以這個為單位，換尺寸只要改輸出大小。
GRID = 100


def _draw_cup(draw: ImageDraw.ImageDraw, k: float, stroke: float) -> None:
    """正視的杯子。

    app icon 的斜視橢圓杯口在 18pt 下會糊成一團——線寬幾乎等於橢圓的高度。
    這裡只留能活下來的結構：一條杯口、一個碗、一個把手。
    """

    width = round(stroke)
    # 杯口
    draw.line([(22 * k, 46 * k), (70 * k, 46 * k)], fill=255, width=width, joint="curve")

    # 碗：從杯口兩端收窄到杯底
    points = []
    steps = 40
    for step in range(steps + 1):
        t = step / steps
        x0, y0 = 26 * k, 48 * k
        x1, y1 = 46 * k, 84 * k
        x2, y2 = 66 * k, 48 * k
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * x1 + t**2 * x2
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t**2 * y2
        points.append((x, y))
    draw.line(points, fill=255, width=width, joint="curve")

    # 把手
    handle = []
    for step in range(33):
        theta = -1.05 + step / 32 * 2.1
        handle.append((72 * k + 10 * k * math.cos(theta), 56 * k + 11 * k * math.sin(theta)))
    draw.line(handle, fill=255, width=round(stroke * 0.85), joint="curve")


def _draw_bars(draw: ImageDraw.ImageDraw, k: float, stroke: float, heights: list[float]) -> None:
    """杯口上的音量條。"""

    base = 38 * k
    for index, height in enumerate(heights):
        x = (33 + index * 13) * k
        draw.line(
            [(x, base), (x, base - height * k)], fill=255, width=round(stroke), joint="curve"
        )


def render(size: int, state: str) -> Image.Image:
    big = size * SUPERSAMPLE
    k = big / GRID
    stroke = big * 0.062
    image = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(image)

    heights = {
        "idle": [6, 11, 8],
        "listening": [11, 21, 15],
        "error": [0, 0, 0],
    }[state]
    if state != "error":
        _draw_bars(draw, k, stroke, heights)
    _draw_cup(draw, k, stroke)
    if state == "error":
        # 一道斜線劃過杯子：服務不通時一眼看得出來。
        draw.line(
            [(22 * k, 80 * k), (74 * k, 24 * k)], fill=255, width=round(stroke * 1.1)
        )

    small = image.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.putalpha(small)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", type=Path)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    for state in ("idle", "listening", "error"):
        for scale, size in ((1, 18), (2, 36), (3, 54)):
            suffix = "" if scale == 1 else f"@{scale}x"
            path = args.outdir / f"MenuBar-{state}{suffix}.png"
            render(size, state).save(path)
        print(f"{state}: {args.outdir}/MenuBar-{state}.png (+@2x, @3x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
