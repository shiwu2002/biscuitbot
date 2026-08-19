# 构建 biscuitbot 桌面应用（Windows）。
#
# 产物：
#   - 安装包     output\windows\*.exe
#   - sidecar    src-tauri\binaries\biscuitbot-sidecar\（onedir 目录）
#
# 前置要求：
#   - Python 虚拟环境 .venv（含项目依赖）
#   - Bun 1.x
#   - Rust stable (MSVC 工具链)
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
# 或  .\scripts\build-desktop.ps1

param(
    [switch]$SkipTauri  # 只重建 sidecar + WebUI，跳过 Tauri 壳构建（调试用）
)

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Message) Write-Host $Message }

# 切换到仓库根目录
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Root
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"
$OutputDir = Join-Path $Root "output\windows"
$DistDir = Join-Path $OutputDir "dist"
$WorkDir = Join-Path $OutputDir "build"
$BinariesDir = Join-Path $Root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

Write-Info "==> 1/5 构建 WebUI（webui/dist → biscuitbot/web/dist）"
Set-Location (Join-Path $Root "webui")
bun install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { bun install }
if ($LASTEXITCODE -ne 0) { throw "bun install 失败" }
bun run build
if ($LASTEXITCODE -ne 0) { throw "bun run build 失败" }
Set-Location $Root

Write-Info "==> 2/5 准备 PyInstaller"
if (-not (Test-Path $PyInstaller)) {
    Write-Info "    安装 pyinstaller..."
    & $VenvPython -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -m pip install --index-url https://pypi.org/simple pyinstaller
    }
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller 安装失败" }
}

Write-Info "==> 3/5 打包 gateway sidecar（PyInstaller onedir）"
# 剔除与 Python 解释器无关的运行时第三方依赖（渠道 SDK / GUI）；
# prompt_toolkit 在 cli/commands.py 顶部被导入，必须保留。
# 注意：不要剔除渠道 SDK（lark_oapi / dingtalk_stream / socketio / botpy 等）——
# 渠道模块是动态导入的（见 biscuitbot/channels/registry.py），PyInstaller 静态
# 分析看不到，必须用 --collect-submodules biscuitbot.channels 显式收集，并保留
# 其依赖的 SDK，否则打包后桌面端会缺渠道。
$Excludes = @(
    "--exclude-module", "telegram",
    "--exclude-module", "telegram.ext",
    "--exclude-module", "slack_sdk",
    "--exclude-module", "discord",
    "--exclude-module", "matrix_nio",
    "--exclude-module", "wechatpy",
    "--exclude-module", "questionary",
    "--exclude-module", "pymupdf",
    "--exclude-module", "qrcode"
)
# TTS 适配器与 SDK 通过字符串/函数内动态导入（tts_registry.load_adapter 用
# import_module、provider 内函数级 import dashscope/edge_tts），PyInstaller
# 静态分析捕捉不到，须显式收集，否则桌面端 text_to_speech 会 ModuleNotFoundError
# 或报「未安装 xxx SDK」。dashscope 用 --collect-all 以连其 tts_v2 子模块与
# websocket-client 依赖一并收进。
$HiddenImports = @(
    "--hidden-import", "biscuitbot.providers.tts",
    "--collect-all", "dashscope"
)
# 工具模块由 ToolLoader 动态导入（pkgutil.iter_modules + import_module），
# PyInstaller 静态分析捕捉不到，须显式收集，否则桌面端缺 cinematic_director /
# discover / spawn / employee / employee_discover / knowledge / search /
# long_task / text_to_speech 等按需发现工具（含 _cinematic 下划线子包）。

& $VenvPython -m PyInstaller --noconfirm --clean --onedir --windowed `
    --distpath "$DistDir" `
    --workpath "$WorkDir" `
    --paths "$Root" `
    --collect-submodules biscuitbot.channels `
    --collect-submodules biscuitbot.agent.tools `
    --name biscuitbot-sidecar `
    --add-data "biscuitbot/web/dist;biscuitbot/web/dist" `
    --add-data "biscuitbot/templates;biscuitbot/templates" `
    --add-data "biscuitbot/skills;biscuitbot/skills" `
    --add-data "biscuitbot/agent/tools/docs;biscuitbot/agent/tools/docs" `
    @HiddenImports `
    @Excludes `
    scripts/desktop_sidecar_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

New-Item -ItemType Directory -Force -Path $BinariesDir | Out-Null
# onedir：整体复制目录（可执行文件 + _internal\），sidecar 启动时按相对路径找依赖
$SidecarDir = Join-Path $BinariesDir "biscuitbot-sidecar"
if (Test-Path $SidecarDir) { Remove-Item -Recurse -Force $SidecarDir }
Copy-Item -Recurse -Force (Join-Path $DistDir "biscuitbot-sidecar") $SidecarDir
Write-Info "    sidecar → src-tauri\binaries\biscuitbot-sidecar\"

if ($SkipTauri) {
    Write-Info "（--SkipTauri 已跳过第 4/5、5/5 步）"
    return
}

Write-Info "==> 4/5 准备 Tauri 壳依赖与图标（幂等）"
Set-Location (Join-Path $Root "src-tauri")
bun install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { bun install }
if ($LASTEXITCODE -ne 0) { throw "bun install 失败" }
if (-not (Test-Path "icons\icon.ico")) {
    bun x tauri icon ..\images\biscuitbot_icon_transparent.png -o icons
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }
}
Set-Location $Root

Write-Info "==> 5/5 Tauri 构建（release，NSIS 安装包）"
Set-Location (Join-Path $Root "src-tauri")
bun run tauri build --bundles nsis
if ($LASTEXITCODE -ne 0) { throw "tauri build 失败" }
Set-Location $Root

# 将最终安装包复制到输出目录（NSIS 安装包仍留在 src-tauri\target 作为构建缓存）
$NsisDir = Join-Path $Root "src-tauri\target\release\bundle\nsis"
Copy-Item -Path (Join-Path $NsisDir "*.exe") -Destination $OutputDir -Force

Write-Host ""
Write-Host "构建完成！产物："
Get-ChildItem $OutputDir -Filter *.exe | ForEach-Object { Write-Host "  $($_.FullName)" }
