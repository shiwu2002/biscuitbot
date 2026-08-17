#!/usr/bin/env bash
# 构建 biscuitbot 桌面应用（macOS / Linux）。
#
# 产物：
#   - 应用包    src-tauri/target/release/bundle/macos/*.app（arm64）
#   - 应用包    src-tauri/target/x86_64-apple-darwin/release/bundle/macos/*.app（--intel）
#   - 安装镜像  src-tauri/target/*/release/bundle/dmg/*.dmg（macOS）
#   - sidecar   src-tauri/binaries/biscuitbot-sidecar/（onedir 目录）
#
# 前置要求：
#   - Python 虚拟环境 .venv（含项目依赖）
#   - 构建 x86_64（--intel）：需 ~/.venvs/biscuitbot-x86 这个 x86_64 Python 环境
#   - Bun 1.x
#   - Rust stable + Xcode Command Line Tools（macOS）或对应平台工具链
#
# 用法：
#   bash scripts/build-desktop.sh            # arm64（Apple Silicon）
#   bash scripts/build-desktop.sh --intel    # x86_64（Intel / Rosetta）

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

# 目标架构与对应构建环境（默认 arm64；--intel 切到 x86_64 交叉构建）
TARGET_TRIPLE="aarch64-apple-darwin"
PYTHON="$ROOT/.venv/bin/python"
DIST_DIR="$ROOT/dist"
WORK_DIR="$ROOT/build"
RELEASE_SUBPATH="release"        # target/<subpath>：arm64 走 target/release
TAURI_BUILD_ARGS=()              # 额外传给 `tauri build` 的参数

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intel)
      # 交叉编译需要 rustup 工具链（含 x86_64-apple-darwin std）；Homebrew 的
      # cargo/rustc 排在 PATH 前会遮蔽 rustup shim，且 Homebrew rust 只带宿主
      # (aarch64) std，导致 `can't find crate for core/std`。把 rustup 提前。
      export PATH="$HOME/.cargo/bin:$PATH"
      TARGET_TRIPLE="x86_64-apple-darwin"
      PYTHON="$HOME/.venvs/biscuitbot-x86/bin/python"
      DIST_DIR="$ROOT/dist-x86"
      WORK_DIR="$ROOT/build-x86"
      RELEASE_SUBPATH="x86_64-apple-darwin/release"
      # 独立 bundle identifier + 产品名，避免与 arm64 版在 macOS 上“撞名”
      TAURI_BUILD_ARGS=(--target "$TARGET_TRIPLE" --config "$ROOT/src-tauri/tauri.x64.conf.json")
      ;;
    *)
      echo "未知参数: $1（支持 --intel）" >&2
      exit 2
      ;;
  esac
  shift
done

echo "==> 1/5 构建 WebUI（webui/dist → biscuitbot/web/dist）"
cd "$ROOT/webui"
bun install --frozen-lockfile || bun install
bun run build
cd "$ROOT"

echo "==> 2/5 准备 PyInstaller（${TARGET_TRIPLE}）"
if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  echo "    安装 pyinstaller..."
  "$PYTHON" -m pip install pyinstaller 2>/dev/null || \
    "$PYTHON" -m pip install --index-url https://pypi.org/simple pyinstaller
fi

echo "==> 3/5 打包 gateway sidecar（PyInstaller onedir，${TARGET_TRIPLE}）"
# 与 Python 解释器无关的运行时第三方依赖（GUI）被剔除；prompt_toolkit 在
# cli/commands.py 顶部被导入，必须保留。渠道 SDK（lark_oapi / dingtalk_stream /
# socketio / botpy 等）不要剔除：渠道模块是动态导入的，需 --collect-submodules
# biscuitbot.channels 显式收集，并保留其依赖的 SDK。
EXCLUDES=(
  --exclude-module telegram
  --exclude-module telegram.ext
  --exclude-module slack_sdk
  --exclude-module discord
  --exclude-module matrix_nio
  --exclude-module wechatpy
  --exclude-module pywebview
  --exclude-module questionary
  --exclude-module pymupdf
  --exclude-module qrcode
  --exclude-module PySide6
  --exclude-module PyQt6
)
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --paths "$ROOT" \
  --collect-submodules biscuitbot.channels \
  --name biscuitbot-sidecar \
  --add-data "biscuitbot/web/dist:biscuitbot/web/dist" \
  --add-data "biscuitbot/templates:biscuitbot/templates" \
  --add-data "biscuitbot/skills:biscuitbot/skills" \
  "${EXCLUDES[@]}" \
  scripts/desktop_sidecar_main.py

mkdir -p "$ROOT/src-tauri/binaries"
# onedir：整体复制目录（可执行文件 + _internal/），sidecar 启动时按相对路径找依赖
rm -rf "$ROOT/src-tauri/binaries/biscuitbot-sidecar"
cp -R "$DIST_DIR/biscuitbot-sidecar" "$ROOT/src-tauri/binaries/biscuitbot-sidecar"
echo "    sidecar → src-tauri/binaries/biscuitbot-sidecar/"

echo "==> 4/5 准备 Tauri 壳依赖与图标（幂等）"
cd "$ROOT/src-tauri"
bun install --frozen-lockfile || bun install
if [ ! -f icons/icon.icns ]; then
  bun x tauri icon ../images/biscuitbot_icon_transparent.png -o icons
fi
cd "$ROOT"

echo "==> 5/5 Tauri 构建（release，${TARGET_TRIPLE}）"
cd "$ROOT/src-tauri"
bun x tauri build "${TAURI_BUILD_ARGS[@]}"
cd "$ROOT"

echo
echo "构建完成！产物："
ls -1 "$ROOT/src-tauri/target/$RELEASE_SUBPATH/bundle/"*/ 2>/dev/null | sed 's/^/  /'
