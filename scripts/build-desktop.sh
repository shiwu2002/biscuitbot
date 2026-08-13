#!/usr/bin/env bash
# 构建 biscuitbot 桌面应用（macOS / Linux）。
#
# 产物：
#   - 应用包    src-tauri/target/release/bundle/macos/*.app（macOS）
#   - 安装镜像  src-tauri/target/release/bundle/dmg/*.dmg（macOS）
#   - sidecar   src-tauri/binaries/biscuitbot-sidecar-<target-triple>
#
# 前置要求：
#   - Python 虚拟环境 .venv（含项目依赖）
#   - Bun 1.x
#   - Rust stable + Xcode Command Line Tools（macOS）或对应平台工具链
#
# 用法：bash scripts/build-desktop.sh

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "==> 1/5 构建 WebUI（webui/dist → biscuitbot/web/dist）"
cd "$ROOT/webui"
bun install --frozen-lockfile || bun install
bun run build
cd "$ROOT"

echo "==> 2/5 准备 PyInstaller"
if ! .venv/bin/pyinstaller --version >/dev/null 2>&1; then
  echo "    安装 pyinstaller..."
  .venv/bin/pip install pyinstaller 2>/dev/null || \
    .venv/bin/pip install --index-url https://pypi.org/simple pyinstaller
fi

echo "==> 3/5 打包 gateway sidecar（PyInstaller one-file）"
# 与 Python 解释器无关的运行时第三方依赖（渠道 SDK / GUI）被剔除；
# prompt_toolkit 在 cli/commands.py 顶部被导入，必须保留。
EXCLUDES=(
  --exclude-module telegram
  --exclude-module telegram.ext
  --exclude-module slack_sdk
  --exclude-module lark_oapi
  --exclude-module discord
  --exclude-module matrix_nio
  --exclude-module python_socketio
  --exclude-module socketio
  --exclude-module qq_botpy
  --exclude-module wechatpy
  --exclude-module dingtalk_stream
  --exclude-module wecom_aibot_sdk_python
  --exclude-module pywebview
  --exclude-module questionary
  --exclude-module pymupdf
  --exclude-module qrcode
  --exclude-module PySide6
  --exclude-module PyQt6
)
# 平台相关的 --add-data 分隔符：macOS/Linux 用冒号，Windows 用分号
DATA_SEP=":"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) DATA_SEP=";" ;;
esac
TRIPLE="$(rustc -vV 2>/dev/null | sed -n 's/^host: //p' || echo 'aarch64-apple-darwin')"

.venv/bin/pyinstaller --noconfirm --clean --onefile --windowed \
  --paths "$ROOT" \
  --name biscuitbot-sidecar \
  --add-data "biscuitbot/web/dist:biscuitbot/web/dist" \
  --add-data "biscuitbot/templates:biscuitbot/templates" \
  --add-data "biscuitbot/skills:biscuitbot/skills" \
  "${EXCLUDES[@]}" \
  scripts/desktop_sidecar_main.py

mkdir -p "$ROOT/src-tauri/binaries"
if [ "$DATA_SEP" = ";" ]; then
  SIDECAR_NAME="biscuitbot-sidecar-$TRIPLE.exe"
else
  SIDECAR_NAME="biscuitbot-sidecar-$TRIPLE"
fi
cp "$ROOT/dist/biscuitbot-sidecar" "$ROOT/src-tauri/binaries/$SIDECAR_NAME"
echo "    sidecar → src-tauri/binaries/$SIDECAR_NAME"

echo "==> 4/5 准备 Tauri 壳依赖与图标（幂等）"
cd "$ROOT/src-tauri"
bun install --frozen-lockfile || bun install
if [ ! -f icons/icon.icns ]; then
  bun x tauri icon ../images/biscuitbot_icon_transparent.png -o icons
fi
cd "$ROOT"

echo "==> 5/5 Tauri 构建（release）"
cd "$ROOT/src-tauri"
bun run tauri build
cd "$ROOT"

echo
echo "构建完成！产物："
ls -1 "$ROOT/src-tauri/target/release/bundle/"*/ 2>/dev/null | sed 's/^/  /'
