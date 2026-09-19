#!/usr/bin/env python3
"""選單列圖示（18pt template）。

app 圖示是實心插畫，縮到 18pt 會糊成一團，所以選單列另外畫線稿。

這個尺寸的設計約束很硬：
- 細線會在非 Retina 上消失，太粗又會讓封閉形狀糊成一塊。1.5pt 是實測
  下來還能看出杯子輪廓的下限。
- 把手如果畫在杯身外面當獨立弧線，18pt 下會變成一顆脫節的黑點。這裡
  改成畫一個環、再把和杯身重疊的內側挖掉，讀起來才是「接在杯子上」。
- 三個狀態必須一眼分得出來，不能只差幾個像素。

狀態：idle 低蒸氣、listening 高蒸氣、error 杯上打叉。
error 不用傳統的斜線——18pt 下斜線會把杯子整個蓋掉，看不出是什麼。
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw

S = 18          # 設計格點，單位 pt
SS = 16         # 超取樣倍率
N = S * SS


def px(v: float) -> float:
    return v * SS


def _canvas() -> Image.Image:
    # L 模式直接當覆蓋率/alpha 用，最後再倒進 RGBA 的 alpha channel。
    return Image.new("L", (N, N), 0)


def _stroke(d: ImageDraw.ImageDraw, pts, w: float, fill: int) -> None:
    """圓端點線段。Pillow 的 line 不畫端點圓角，得自己補。"""
    d.line([(px(x), px(y)) for x, y in pts], fill=fill, width=int(px(w)), joint="curve")
    r = px(w) / 2
    for x, y in pts:
        d.ellipse([px(x) - r, px(y) - r, px(x) + r, px(y) + r], fill=fill)


def _cup(d: ImageDraw.ImageDraw, cx=7.8, top=5.8, bot=14.8, ht=4.9, hb=3.3, wall=1.5) -> None:
    # 先畫把手環，再把杯身內側的部分挖掉，環才會看起來接在杯子上。
    d.ellipse([px(cx + ht - 3.1), px(6.0), px(cx + ht + 2.7), px(11.8)], fill=255)
    d.ellipse([px(cx + ht - 1.8), px(7.3), px(cx + ht + 1.4), px(10.5)], fill=0)
    d.rectangle([px(cx - 1), px(5.0), px(cx + ht - 0.4), px(12.0)], fill=0)

    body = [(cx - ht, top), (cx + ht, top), (cx + hb, bot), (cx - hb, bot)]
    d.polygon([(px(x), px(y)) for x, y in body], fill=255)
    k = wall
    inner = [
        (cx - ht + k, top + k),
        (cx + ht - k, top + k),
        (cx + hb - k * 0.8, bot - k),
        (cx - hb + k * 0.8, bot - k),
    ]
    d.polygon([(px(x), px(y)) for x, y in inner], fill=0)


def render(state: str) -> Image.Image:
    img = _canvas()
    d = ImageDraw.Draw(img)
    _cup(d)

    if state == "error":
        _stroke(d, [(6.2, 1.6), (9.4, 4.4)], 1.5, 255)
        _stroke(d, [(9.4, 1.6), (6.2, 4.4)], 1.5, 255)
        return img

    heights = {"idle": [1.5, 2.4, 1.5], "listening": [3.2, 5.0, 3.2]}[state]
    for i, h in enumerate(heights):
        x = 5.0 + i * 2.8
        _stroke(d, [(x, 4.6), (x, 4.6 - h)], 1.5, 255)
    return img


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "Resources/icons")
    out.mkdir(parents=True, exist_ok=True)

    for state in ("idle", "listening", "error"):
        big = render(state)
        for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
            side = S * scale
            rgba = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            rgba.putalpha(big.resize((side, side), Image.LANCZOS))
            rgba.save(out / f"MenuBar-{state}{suffix}.png")
        print(f"{state}: {out}/MenuBar-{state}.png (+@2x, @3x)")


if __name__ == "__main__":
    main()
