#!/usr/bin/env bash
# 构建 biscuitbot 桌面应用（macOS / Linux）。
#
# 产物：
#   - 应用包 / 镜像  output/mac/*.app + *.dmg（arm64）
#   - 应用包 / 镜像  output/mac-x86/*.app + *.dmg（--intel）
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
OUTPUT_DIR="$ROOT/output/mac"
DIST_DIR="$OUTPUT_DIR/dist"
WORK_DIR="$OUTPUT_DIR/build"
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
      OUTPUT_DIR="$ROOT/output/mac-x86"
      DIST_DIR="$OUTPUT_DIR/dist"
      WORK_DIR="$OUTPUT_DIR/build"
      RELEASE_SUBPATH="x86_64-apple-darwin/release"
      # 应用名 / identifier 与 arm64 版一致（biscuitbot / com.biscuitbot.desktop），
      # 仅交叉编译目标不同；DMG 文件名按架构区分（_x64 vs _aarch64）。
      TAURI_BUILD_ARGS=(--target "$TARGET_TRIPLE")
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
  --exclude-module questionary
  --exclude-module pymupdf
  --exclude-module qrcode
)
# TTS 适配器与 SDK 通过字符串/函数内动态导入（tts_registry.load_adapter 用
# import_module、provider 内函数级 import dashscope/edge_tts），PyInstaller
# 静态分析捕捉不到，须显式收集，否则桌面端 text_to_speech 会 ModuleNotFoundError
# 或报「未安装 xxx SDK」。dashscope 用 --collect-all 以连其 tts_v2 子模块与
# websocket-client 依赖一并收进。
HIDDEN_IMPORTS=(
  --hidden-import biscuitbot.providers.tts
  --collect-all dashscope
)
# 工具模块由 ToolLoader 动态导入（pkgutil.iter_modules + import_module），
# PyInstaller 静态分析捕捉不到，须显式收集，否则桌面端缺 cinematic_director /
# discover / spawn / employee / employee_discover / knowledge / search /
# long_task / text_to_speech 等按需发现工具（含 _cinematic 下划线子包）。
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --paths "$ROOT" \
  --collect-submodules biscuitbot.channels \
  --collect-submodules biscuitbot.agent.tools \
  --collect-submodules biscuitbot.agent.tools \
  --name biscuitbot-sidecar \
  --add-data "biscuitbot/web/dist:biscuitbot/web/dist" \
  --add-data "biscuitbot/templates:biscuitbot/templates" \
  --add-data "biscuitbot/skills:biscuitbot/skills" \
  --add-data "biscuitbot/agent/tools/docs:biscuitbot/agent/tools/docs" \
  "${HIDDEN_IMPORTS[@]}" \
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
# bash 3.2 下 `set -u` 会把空数组的 "${arr[@]}" 判为 unbound，须先判长度
if [[ ${#TAURI_BUILD_ARGS[@]} -gt 0 ]]; then
  bun x tauri build "${TAURI_BUILD_ARGS[@]}"
else
  bun x tauri build
fi
cd "$ROOT"

# 清理 tauri-bundler 在 bundle/dmg/ 遗留的卷图标临时文件。
# tauri-bundler 的 create_icns_file（macos/icon.rs）会把 icons/icon.icns 复制到
# bundle/dmg/icon.icns 用作 `--volicon` 源（DMG 内会正确转为隐藏的 .VolumeIcon.icns），
# 但打包结束后不删除，导致 icon.icns 泄漏到产物目录；bundle_dmg.sh 同样是 create-dmg
# 的中间脚本。这里统一清理，让 bundle/dmg/ 只保留 *.dmg。
DMG_DIR="$ROOT/src-tauri/target/$RELEASE_SUBPATH/bundle/dmg"
if [ -d "$DMG_DIR" ]; then
  rm -f "$DMG_DIR/icon.icns" "$DMG_DIR/bundle_dmg.sh"
fi

# 将最终产物（.app / .dmg）复制到输出目录；target 内仍保留构建缓存。
mkdir -p "$OUTPUT_DIR"
BUNDLE_DIR="$ROOT/src-tauri/target/$RELEASE_SUBPATH/bundle"
cp -R "$BUNDLE_DIR/macos/"*.app "$OUTPUT_DIR/" 2>/dev/null || true
cp -f "$BUNDLE_DIR/dmg/"*.dmg "$OUTPUT_DIR/" 2>/dev/null || true

echo
echo "构建完成！产物："
ls -1 "$OUTPUT_DIR/" 2>/dev/null | sed 's/^/  /'
