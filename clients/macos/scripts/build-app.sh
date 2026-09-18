#!/bin/bash
# 組出可執行的 TEA ASR.app。
#
# 用 Command Line Tools 的 Swift，不需要接受 Xcode 授權條款，也不需要 sudo。
# 簽章是 ad-hoc：本機可執行，但不能發給別人（那需要開發者憑證與公證）。
set -euo pipefail

cd "$(dirname "$0")/.."
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Library/Developer/CommandLineTools}"

CONFIG="${1:-release}"
APP="build/TEA ASR.app"

echo "==> swift build -c $CONFIG"
swift build -c "$CONFIG"

BINARY="$(swift build -c "$CONFIG" --show-bin-path)/TeaASRClient"

echo "==> 組裝 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BINARY" "$APP/Contents/MacOS/TeaASRClient"
cp Resources/Info.plist "$APP/Contents/Info.plist"

echo "==> ad-hoc 簽章"
# TCC 用簽章識別 app。沒有穩定的簽章，每次重建都會重新詢問麥克風權限。
codesign --force --sign - --identifier com.tea-asr.client "$APP"
codesign --verify --verbose=1 "$APP"

echo
echo "完成：$(pwd)/$APP"
echo "執行：open '$(pwd)/$APP'"
