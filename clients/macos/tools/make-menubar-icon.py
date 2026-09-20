#!/usr/bin/env python3
"""選單列圖示（template），從 tools/source-menubar.png 裁切而來。

來源是一張三個狀態並排的深色稿（idle / listening / error），下方另有
淺色外觀預覽。template 只需上排共用的 shape alpha，下排預覽不需輸出。

這個尺寸的注意事項：
- macOS 在 Retina 上實際顯示的是 @2x（36px），1x 只有外接非 Retina 螢幕
  會用到。造型要以 36px 為主要驗收標準，但 18px 仍要能看出是杯子。
- template image 只有 alpha 覆蓋率有意義；系統會依淺色/深色模式自動染色，
  被點選時自動反白。來源稿的上排杯子本身已有正確的 alpha 邊緣，必須
  直接取用 alpha，而不是把深灰 RGB 當成灰階遮罩。後者會讓原本實心的杯身
  只剩約 92% 不透明，看起來像被洗淡的透明圖示。
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
#: 來源圖下半部是淺色外觀預覽，template 只取上半部。
CONTENT_TOP_RATIO = 0.52
# AI 來源稿邊緣可能有 alpha=1 的散落像素；它們不應該決定裁切框。
SOURCE_ALPHA_TOLERANCE = 16


def _alpha_bbox(cell: Image.Image, tol: int = SOURCE_ALPHA_TOLERANCE):
    """Return the visible bounds without treating near-transparent specks as ink."""
    return cell.point(lambda value: 255 if value > tol else 0).getbbox()


def _cells(source: pathlib.Path) -> list[Image.Image]:
    # AppKit template images use the alpha channel as their mask.  The source
    # contains both dark and light rows, so RGB/luminance is not a reliable
    # source of opacity: the dark row is intentionally RGB (20, 20, 19), not
    # pure black.  Reading alpha preserves the solid cup artwork and its
    # anti-aliased edges while discarding the lower light-appearance preview.
    sheet = Image.open(source).convert("RGBA")
    w, h = sheet.size
    top = sheet.crop((0, 0, w, int(h * CONTENT_TOP_RATIO)))
    out = []
    for i in range(len(STATES)):
        cell = top.crop((i * w // 3, 0, (i + 1) * w // 3, top.height))
        alpha = cell.getchannel("A")
        box = _alpha_bbox(alpha)
        out.append(alpha.crop(box) if box else alpha)
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
        mask = Image.new("L", (side, side), 0)
        mask.paste(
            cell.resize((w, h), Image.Resampling.LANCZOS),
            ((side - w) // 2, baseline - h),
        )
        template = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        template.putalpha(mask)
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
