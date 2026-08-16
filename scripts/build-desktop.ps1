# 构建 biscuitbot 桌面应用（Windows）。
#
# 产物：
#   - 安装包     src-tauri\target\release\bundle\nsis\*.exe
#   - sidecar    src-tauri\binaries\biscuitbot-sidecar-<target-triple>.exe
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
$DistDir = Join-Path $Root "dist"
$BinariesDir = Join-Path $Root "src-tauri\binaries"

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

Write-Info "==> 3/5 打包 gateway sidecar（PyInstaller one-file）"
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
    "--exclude-module", "pywebview",
    "--exclude-module", "questionary",
    "--exclude-module", "pymupdf",
    "--exclude-module", "qrcode",
    "--exclude-module", "PySide6",
    "--exclude-module", "PyQt6"
)

$TripleLine = & rustc -vV 2>$null | Select-String '^host: '
$Triple = if ($TripleLine) { ($TripleLine -split ": ")[1].Trim() } else { "x86_64-pc-windows-msvc" }

& $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed `
    --paths "$Root" `
    --collect-submodules biscuitbot.channels `
    --name biscuitbot-sidecar `
    --add-data "biscuitbot/web/dist;biscuitbot/web/dist" `
    --add-data "biscuitbot/templates;biscuitbot/templates" `
    --add-data "biscuitbot/skills;biscuitbot/skills" `
    @Excludes `
    scripts/desktop_sidecar_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

New-Item -ItemType Directory -Force -Path $BinariesDir | Out-Null
$SidecarName = "biscuitbot-sidecar-$Triple.exe"
Copy-Item -Force (Join-Path $DistDir "biscuitbot-sidecar.exe") (Join-Path $BinariesDir $SidecarName)
Write-Info "    sidecar → src-tauri\binaries\$SidecarName"

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

Write-Host ""
Write-Host "构建完成！产物："
Get-ChildItem (Join-Path $Root "src-tauri\target\release\bundle") -Recurse -Include *.exe | ForEach-Object { Write-Host "  $($_.FullName)" }
