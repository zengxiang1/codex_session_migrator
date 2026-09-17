#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_ROOT="$ROOT_DIR/.macos-intel-build"
VENV_DIR="$BUILD_ROOT/venv"
WORK_DIR="$BUILD_ROOT/pyinstaller"
DIST_DIR="$ROOT_DIR/dist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "错误：此脚本必须在 macOS 上运行。" >&2
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "错误：当前不是 Intel Mac（x86_64）。请使用 Intel Mac 或 GitHub macos-15-intel runner。" >&2
  exit 1
fi

mkdir -p "$BUILD_ROOT" "$DIST_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "pyinstaller==6.16.0"

cd "$ROOT_DIR"
"$VENV_DIR/bin/python" -X dev -m unittest -v test_codex_session_migrator.py

export MACOSX_DEPLOYMENT_TARGET="11.0"
"$VENV_DIR/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --target-arch x86_64 \
  --name CodexSessionMigrator \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$BUILD_ROOT" \
  desktop_app.py

APP_PATH="$DIST_DIR/CodexSessionMigrator.app"
EXEC_PATH="$APP_PATH/Contents/MacOS/CodexSessionMigrator"
ZIP_PATH="$DIST_DIR/CodexSessionMigrator-macOS-Intel-x86_64-v0.2.0.zip"

file "$EXEC_PATH"
if ! file "$EXEC_PATH" | grep -q "x86_64"; then
  echo "错误：构建产物不是 x86_64。" >&2
  exit 1
fi

# Ad-hoc signing gives the bundle a consistent code signature. It is not
# Apple notarization and does not identify a verified developer.
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ZIP_PATH"
shasum -a 256 "$ZIP_PATH"

echo
echo "构建完成：$ZIP_PATH"
echo "首次打开若被 Gatekeeper 拦截，请在 Finder 中右键应用并选择“打开”。"
