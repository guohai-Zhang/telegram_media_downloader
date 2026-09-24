#!/usr/bin/env bash
# Build TelegramDownloader.app and a .dmg for Apple Silicon Macs (macOS 11+).
# Usage: make mac-app
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
HERE="packaging/macos"
VENV="build/venv-gui"
MIN_MACOS="11.0"
APP="dist/TelegramDownloader.app"

if [ "$(uname -m)" != "arm64" ]; then
  echo "This build targets Apple Silicon; run it on an arm64 Mac." >&2
  exit 1
fi

# uv-managed CPython is built for macOS 11; Homebrew's Python requires the build machine's macOS.
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --seed --python 3.11 --python-preference only-managed "$VENV"
fi
PY="$VENV/bin/python"
export MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS"
"$PY" -m pip install -q -r requirements-gui.txt

VERSION="$("$PY" -c 'from utils import __version__; print(__version__)')"
DMG="dist/TelegramDownloader-${VERSION}-arm64.dmg"

"$PY" gen_filter_cache.py
[ -f "$HERE/icon.icns" ] || "$PY" "$HERE/make_icon.py" "$HERE/icon.icns"

rm -rf build/tdl_gui "$APP"
"$VENV/bin/pyinstaller" --noconfirm --clean --distpath dist --workpath build/tdl_gui "$HERE/tdl_gui.spec"

# unsigned arm64 apps downloaded from the internet show "is damaged"; ad-hoc signing avoids that
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

"$PY" "$HERE/check_minos.py" "$APP" "$MIN_MACOS"

if find "$APP" \( -name "config.yaml" -o -name "data.yaml" -o -name "*.session" \) | grep -q .; then
  echo "Refusing to package user config or sessions" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Telegram 下载器" -srcfolder "$STAGE" -format UDZO -ov "$DMG" >/dev/null
rm -rf "$STAGE"
echo "Built $DMG"
