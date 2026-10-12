"""用「夏奈儿 Agent」新 logo 重生成项目全部图标。

- 源：images/app_icon.png（1024 圆角方块瓦片母版，透明留白已裁边居中）
- 产物1：images/codex_icon.png   —— 同源 1024 母版（历史命名，仍是
    build-desktop.ps1/.sh 在 icons/icon.ico 缺失时的兜底图标源）
- 产物2：images/ 母版三件套        —— 1807 瓦片 x2 + 白色剪影
- 产物3：webui/public/brand/*     —— WebUI favicon + 页内 logo
- 产物4：src-tauri/window/robot.png —— 桌面启动闪屏徽记
- 产物5：src-tauri/icons/         —— Tauri 各平台图标（由 tauri icon 生成，delegate）

用法：python scripts/regen_icons.py     （默认重生成以上 1-4）
      python scripts/regen_icons.py --tauri        （额外调用 tauri icon）
      python scripts/regen_icons.py --splash-only  （只重生成桌面闪屏图）
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
APP_ICON = ROOT / "images" / "app_icon.png"
# 历史命名：构建脚本兜底图标源仍指向 codex_icon.png，这里保持同内容，
# 避免旧文件名指向过时品牌。
LEGACY_MASTER = ROOT / "images" / "codex_icon.png"
BRAND = ROOT / "webui" / "public" / "brand"
TAURI_ICONS = ROOT / "src-tauri" / "icons"

# images/ 母版尺寸（沿用旧约定）
ICON_SIZE = 1807
APPLE = 180           # xianaibot_apple_touch.png
FAV32 = 32            # xianaibot_favicon_32.png
SIDE_ICON = 256       # brand/xianaibot_icon.png（侧边栏/顶栏 20px 展示，256 覆盖 HiDPI）
ICO_SIZES = [16, 24, 32, 48, 64]

# 桌面启动闪屏（src-tauri/window/index.html）里的徽记
SPLASH = ROOT / "src-tauri" / "window"
SPLASH_PX = 512  # 闪屏里以 76px CSS 显示，512 足够覆盖 HiDPI

# 徽记裁剪参数：瓦片内去掉底部「夏奈儿 Agent」文字，只取中心光环图案。
# 中心在瓦片高度 46.5% 处，边长约 0.70 * 瓦片边（实测校准值）。
EMBLEM_SIDE_RATIO = 0.70
EMBLEM_CENTER_Y_RATIO = 0.465


def load_tile() -> Image.Image:
    if not APP_ICON.exists():
        raise SystemExit(
            f"找不到源母版 {APP_ICON}，已中止。\n"
            "请先放置新的 1024 方形瓦片母版（裁边居中、透明底）。"
        )
    return Image.open(APP_ICON).convert("RGBA")


def emblem(tile: Image.Image, size: int) -> Image.Image:
    """中心光环徽记：瓦片裁掉底部文字后的正方形区域。"""
    w, h = tile.size
    side = int(min(w, h) * EMBLEM_SIDE_RATIO)
    cx, cy = w / 2, h * EMBLEM_CENTER_Y_RATIO
    box = (
        int(cx - side / 2),
        int(cy - side / 2),
        int(cx + side / 2),
        int(cy + side / 2),
    )
    return tile.crop(box).resize((size, size), Image.Resampling.LANCZOS)


def save_sized(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.Resampling.LANCZOS)


def white_silhouette(img: Image.Image, size: int) -> Image.Image:
    """白色剪影版（深色背景用的 logo）：保留 alpha 形状，填充为白。"""
    img = save_sized(img, size)
    canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", img.size, (255, 255, 255, 255)), (0, 0), img.split()[-1])
    return canvas


def regen_masters(tile: Image.Image) -> None:
    """images/ 下的母版图，全部重生成为新瓦片。"""
    for name in ("xianaibot_icon_transparent.png", "xianaibot_logo.png"):
        save_sized(tile, ICON_SIZE).save(ROOT / "images" / name)
        print(f"  images/{name}  ← {ICON_SIZE}x{ICON_SIZE}")
    white_silhouette(tile, ICON_SIZE).save(ROOT / "images" / "xianaibot_logo_white.png")
    print(f"  images/xianaibot_logo_white.png  ← {ICON_SIZE}x{ICON_SIZE}（白色剪影）")


def regen_brand(tile: Image.Image) -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    jobs = {
        # 小尺寸处用徽记（完整瓦片带文字，缩到 20px 会糊）
        "xianaibot_icon.png": lambda: emblem(tile, SIDE_ICON),
        "xianaibot_favicon_32.png": lambda: emblem(tile, FAV32),
        # apple-touch / 页内大图用完整瓦片
        "xianaibot_apple_touch.png": lambda: save_sized(tile, APPLE),
        "xianaibot_logo.png": lambda: save_sized(tile, ICON_SIZE),
    }
    for name, gen in jobs.items():
        gen().save(BRAND / name)
        print(f"  brand/{name}")

    # favicon.ico：徽记多尺寸 ico
    ico_path = BRAND / "favicon.ico"
    frames = [emblem(tile, s) for s in ICO_SIZES]
    frames[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[1:],
    )
    print(f"  brand/favicon.ico  ← {ICO_SIZES}")


def regen_splash(tile: Image.Image | None = None) -> None:
    """桌面启动闪屏（src-tauri/window/index.html）用的徽记图。"""
    if tile is None:
        if not APP_ICON.exists():
            raise SystemExit(f"找不到 {APP_ICON}，无法生成闪屏图。")
        tile = Image.open(APP_ICON).convert("RGBA")
    SPLASH.mkdir(parents=True, exist_ok=True)
    emblem(tile, SPLASH_PX).save(SPLASH / "robot.png", optimize=True)
    print(f"  src-tauri/window/robot.png  ← {SPLASH_PX}x{SPLASH_PX}（徽记）")


def regen_tauri() -> None:
    if not (ROOT / "src-tauri" / "package.json").exists():
        print("  ! 未找到 src-tauri/package.json，跳过 tauri icon")
        return
    # bun 在 Windows 上是 .CMD shim，Python 子进程需 shell=True 才能解析。
    subprocess.run(
        "bun x tauri icon \"%s\" -o \"%s\"" % (APP_ICON, TAURI_ICONS),
        cwd=str(ROOT / "src-tauri"),
        check=True,
        shell=True,
    )
    print("  src-tauri/icons/  ← tauri icon")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tauri", action="store_true", help="同时重生成 Tauri 平台图标")
    parser.add_argument(
        "--splash-only",
        action="store_true",
        help="只重生成桌面闪屏图（不依赖 images/ 其余产物）",
    )
    args = parser.parse_args()

    if args.splash_only:
        regen_splash()
        return

    tile = load_tile()

    # images/codex_icon.png 与母版同内容，保住构建脚本的兜底图标源语义。
    LEGACY_MASTER.parent.mkdir(parents=True, exist_ok=True)
    tile.save(LEGACY_MASTER)
    print(f"images/{LEGACY_MASTER.name}  ← {tile.size[0]}x{tile.size[1]}")

    regen_masters(tile)
    regen_brand(tile)
    regen_splash(tile)

    if args.tauri:
        regen_tauri()
    else:
        print("  （跳过 tauri icon，如需请加 --tauri）")


if __name__ == "__main__":
    main()
