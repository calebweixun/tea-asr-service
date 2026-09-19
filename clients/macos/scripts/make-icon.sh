#!/bin/bash
# 重新產生 app 圖示與選單列圖示。
#
#   ./scripts/make-icon.sh [來源.png]
#
# 來源預設是 tools/source-cup.png。生成的圖通常是「圓角 icon 畫在不透明方形上」，
# 四角不是透明的；直接拿去當 .icns，每個圓角後面都會有一塊白。這個腳本會裁掉
# 外圍與自帶描邊、套上 Apple 的 squircle 遮罩、內縮到 macOS 的比例，再輸出
# 全尺寸與 .icns。
#
# 只依賴 Python 與 Pillow，不需要 Swift——圖示資產不該因為 Xcode 壞掉就做不出來。
set -euo pipefail

cd "$(dirname "$0")/.."
SOURCE="${1:-tools/source-cup.png}"

python3 tools/make-icon.py "$SOURCE" Resources/icons AppIcon
python3 tools/make-menubar-icon.py Resources/icons

echo
echo "完成。重新建置 app 會帶上新圖示：./scripts/build-app.sh"
