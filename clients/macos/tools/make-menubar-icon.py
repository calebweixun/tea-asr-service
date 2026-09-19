#!/usr/bin/env python3
"""選單列圖示（template），從 tools/source-menubar.png 裁切而來。

來源是一張三個狀態並排的黑白稿（idle / listening / error），外加下方
一層淡淡的倒影——倒影要丟掉，只取上半。

這個尺寸的注意事項：
- macOS 在 Retina 上實際顯示的是 @2x（36px），1x 只有外接非 Retina 螢幕
  會用到。造型要以 36px 為主要驗收標準，但 18px 仍要能看出是杯子。
- template image 只有覆蓋率有意義：黑色 -> 不透明，白底 -> 全透明，
  系統會依淺色/深色模式自動染色，被點選時自動反白。所以來源必須是
  純黑白，不能有灰階背景。
- 三個狀態共用同一個縮放比、並以杯底對齊。否則 listening 那格因為多了
  蒸氣而比較高，等比縮放後杯子會比其他狀態小，切換狀態時會忽大忽小。
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image

STATES = ("idle", "listening", "error")
SIDE = 18
PAD = 0.04
#: 來源圖下半部是倒影，只取上半。
CONTENT_TOP_RATIO = 0.52


def _flatten(path: pathlib.Path) -> Image.Image:
    img = Image.open(path)
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.alpha_composite(img.convert("RGBA"))
    return bg.convert("RGB")


def _ink_bbox(cell: Image.Image, tol: int = 200):
    return cell.convert("L").point(lambda v: 255 if v < tol else 0).getbbox()


def _cells(source: pathlib.Path) -> list[Image.Image]:
    sheet = _flatten(source)
    w, h = sheet.size
    top = sheet.crop((0, 0, w, int(h * CONTENT_TOP_RATIO)))
    out = []
    for i in range(len(STATES)):
        cell = top.crop((i * w // 3, 0, (i + 1) * w // 3, top.height))
        box = _ink_bbox(cell)
        out.append(cell.crop(box) if box else cell)
    return out


def _render(cells: list[Image.Image], side: int) -> list[Image.Image]:
    inner = side * (1 - 2 * PAD)
    scale = min(
        inner / max(c.width for c in cells),
        inner / max(c.height for c in cells),
    )
    baseline = side - round(side * PAD)

    rendered = []
    for cell in cells:
        w = max(1, round(cell.width * scale))
        h = max(1, round(cell.height * scale))
        flat = Image.new("RGBA", (side, side), (255, 255, 255, 255))
        flat.paste(cell.resize((w, h), Image.LANCZOS).convert("RGBA"), ((side - w) // 2, baseline - h))
        template = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        template.putalpha(flat.convert("L").point(lambda v: 255 - v))
        rendered.append(template)
    return rendered


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "Resources/icons")
    source = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "tools/source-menubar.png")
    out.mkdir(parents=True, exist_ok=True)

    cells = _cells(source)
    for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
        for state, img in zip(STATES, _render(cells, SIDE * scale)):
            img.save(out / f"MenuBar-{state}{suffix}.png")
    for state in STATES:
        print(f"{state}: {out}/MenuBar-{state}.png (+@2x, @3x)")


if __name__ == "__main__":
    main()
