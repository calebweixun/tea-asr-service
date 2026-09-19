#!/bin/bash
# 組出可執行的 TEA ASR.app。
#
# 優先用 Command Line Tools 的 Swift：不需要接受 Xcode 授權條款，也不需要 sudo。
# 系統更新後 CLT 可能落後於 Xcode（swift-package 會因缺符號而 abort），
# 這時自動改用 Xcode 內建的工具鏈。
# 簽章是 ad-hoc：本機可執行，但不能發給別人（那需要開發者憑證與公證）。
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG="${1:-release}"
APP="build/TEA ASR.app"

SWIFT=/Library/Developer/CommandLineTools/usr/bin/swift
if ! "$SWIFT" package --help >/dev/null 2>&1; then
  SWIFT="$(xcrun --find swift)"
  echo "==> Command Line Tools 的 Swift 不可用，改用 $SWIFT"
fi

echo "==> swift build -c $CONFIG"
"$SWIFT" build -c "$CONFIG"

BINARY="$("$SWIFT" build -c "$CONFIG" --show-bin-path)/TeaASRClient"

echo "==> 組裝 $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BINARY" "$APP/Contents/MacOS/TeaASRClient"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# The icon is optional: the app still runs without one, it just looks blank.
if [ -f Resources/icons/AppIcon.icns ]; then
  cp Resources/icons/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
  # Menu bar templates: black plus alpha, tinted by the system.
  cp Resources/icons/MenuBar-*.png "$APP/Contents/Resources/" 2>/dev/null || true
else
  echo "（沒有 Resources/icons/AppIcon.icns，app 會用預設空白圖示；"
  echo "  產生方式：./scripts/make-icon.sh <來源.png>）"
fi

echo "==> 簽章"
# TCC binds permissions to the code-signing requirement, not just the bundle
# path. Prefer a Developer ID identity when one is installed so microphone and
# Accessibility grants survive rebuilds. A local machine without a private
# certificate still gets a runnable ad-hoc build, but its cdhash changes when
# the binary changes and macOS may require both permissions to be granted again.
IDENTITIES="$(security find-identity -v -p codesigning 2>/dev/null || true)"
SIGN_IDENTITY="${TEA_ASR_SIGN_IDENTITY:-}"
if [ -z "$SIGN_IDENTITY" ]; then
  while IFS= read -r identity_line; do
    case "$identity_line" in
      *'"Developer ID Application: '*)
        SIGN_IDENTITY="${identity_line#*\"}"
        SIGN_IDENTITY="${SIGN_IDENTITY%%\"*}"
        break
        ;;
    esac
  done <<< "$IDENTITIES"
fi

if [ -n "$SIGN_IDENTITY" ]; then
  codesign --force --sign "$SIGN_IDENTITY" --identifier com.tea-asr.client "$APP"
  echo "==> Developer ID 簽章：$SIGN_IDENTITY"
else
  echo "==> WARNING：找不到 Developer ID Application 憑證，使用 ad-hoc 簽章。" >&2
  echo "    這個 build 可以執行，但重建後 TCC 可能需要重新授權麥克風與輔助使用。" >&2
  echo "    若已安裝憑證，可設定 TEA_ASR_SIGN_IDENTITY，或在 Keychain 加入 Developer ID Application。" >&2
  codesign --force --sign - --identifier com.tea-asr.client "$APP"
fi
codesign --verify --strict --verbose=1 "$APP"

echo
echo "完成：$(pwd)/$APP"
echo "執行：open '$(pwd)/$APP'"
