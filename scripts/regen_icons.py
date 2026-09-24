"""用 Codex 小机器人重生成项目全部图标。

- 源：images/Codex图标介绍 (1).png（非方形，带透明底）
- 产物1：images/codex_icon.png   —— 1024 方形、透明留白的构图完整源图
- 产物2：webui/public/brand/*     —— WebUI favicon + 页内 logo（按原尺寸生成）
- 产物3：src-tauri/icons/         —— Tauri 各平台图标（由 tauri icon 生成，delegate）

用法：python scripts/regen_icons.py     （默认重生成 codex_icon.png 与 brand）
      python scripts/regen_icons.py --tauri        （额外调用 tauri icon）
      python scripts/regen_icons.py --splash-only  （只重生成桌面闪屏图）
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ROBOT = ROOT / "images" / "codex_icon.png"
# 源图名含中文，Windows 下按字节匹配前缀可避免编码问题（中文部分不参与匹配）。
# 必须显式排除 ROBOT 自身：原始源图一旦缺失，Windows 的 Path.glob 大小写不敏感，
# `Codex*` 会命中本脚本的产物 codex_icon.png —— 于是脚本拿自己的成品当源，
# 裁边 + 重采样后再覆盖回去，每跑一次降质一层（不可逆，与配置文件反复被
# 错编码损坏是同一类失效）。
SOURCE = next((p for p in (ROOT / "images").glob("Codex*") if p.name != ROBOT.name), None)
BRAND = ROOT / "webui" / "public" / "brand"
TAURI_ICONS = ROOT / "src-tauri" / "icons"

# 目标方形尺寸 & 留白比例
SQUARE = 1024
MARGIN = 0.045  # 机器人四周各留 ~4.5% 透明边

# 原始 brand 尺寸（照搬，避免改动布局假设）
ICON_SIZE = 1807     # xianaibot_icon.png / xianaibot_logo.png
APPLE = 180          # xianaibot_apple_touch.png
FAV32 = 32           # xianaibot_favicon_32.png
ICO_SIZES = [16, 24, 32, 48, 64]

# 桌面启动闪屏（src-tauri/window/index.html）里的 logo
SPLASH = ROOT / "src-tauri" / "window"
SPLASH_PX = 256  # 闪屏里以 76px CSS 显示，256 足够覆盖 HiDPI


def framed_robot() -> Image.Image:
    """从非方形源生成带透明留白的 1024 方形机器人图。

    原始源图缺失时**拒绝执行**：此时 glob 能匹配到的 `Codex*` 只剩下本脚本
    自己的产物，拿它当源会把成品反复裁边 + 重采样再覆盖回去，属于静默且
    不可逆的降质。宁可直接报错，也不要损坏图标。
    """
    if SOURCE is None:
        raise SystemExit(
            "找不到原始源图 images/Codex*（机器人图），已中止。\n"
            f"注意：images/{ROBOT.name} 是本脚本的产物，不能当源（会被逐次降质）。\n"
            "请先恢复原始源图再运行；若只需重生成桌面闪屏图，用 --splash-only。"
        )
    im = Image.open(SOURCE).convert("RGBA")
    alpha = im.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        im = im.crop(bbox)

    w, h = im.size
    usable = SQUARE * (1 - 2 * MARGIN)
    scale = usable / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    im = im.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (SQUARE, SQUARE), (0, 0, 0, 0))
    canvas.paste(im, ((SQUARE - nw) // 2, (SQUARE - nh) // 2), im)
    return canvas


def save_sized(robot: Image.Image, size: int) -> Image.Image:
    """把机器人缩放到指定方形（透明背景随之缩放）。"""
    return robot.resize((size, size), Image.Resampling.LANCZOS)


def white_silhouette(robot: Image.Image, size: int) -> Image.Image:
    """白色剪影版（深色背景用的 logo）：保留 alpha 形状，填充为白。"""
    img = save_sized(robot, size)
    canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
    canvas.paste(Image.new("RGBA", img.size, (255, 255, 255, 255)), (0, 0), img.split()[-1])
    return canvas


def regen_masters(robot: Image.Image) -> None:
    """images/ 下的母版图（此前是狼），全部重生成机器人。"""
    for name in ("xianaibot_icon_transparent.png", "xianaibot_logo.png"):
        save_sized(robot, ICON_SIZE).save(ROOT / "images" / name)
        print(f"  images/{name}  ← {ICON_SIZE}x{ICON_SIZE}")
    white_silhouette(robot, ICON_SIZE).save(ROOT / "images" / "xianaibot_logo_white.png")
    print(f"  images/xianaibot_logo_white.png  ← {ICON_SIZE}x{ICON_SIZE}（白色剪影）")


def regen_brand(robot: Image.Image) -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    jobs = {
        "xianaibot_icon.png": ICON_SIZE,
        "xianaibot_logo.png": ICON_SIZE,
        "xianaibot_apple_touch.png": APPLE,
        "xianaibot_favicon_32.png": FAV32,
    }
    for name, size in jobs.items():
        img = save_sized(robot, size)
        path = BRAND / name
        img.save(path)
        print(f"  brand/{name}  ← {size}x{size}")

    # favicon.ico：多尺寸 ico
    ico_path = BRAND / "favicon.ico"
    ico_frames = [save_sized(robot, s) for s in ICO_SIZES]
    ico_frames[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=ico_frames[1:],
    )
    print(f"  brand/favicon.ico  ← {ICO_SIZES}")


def regen_splash() -> None:
    """桌面启动闪屏（src-tauri/window/index.html）用的机器人图。

    直接取 images/codex_icon.png —— 它本身就是「已裁边 + 4.5% 留白」的 1024
    方形成品，所以不需要（也不应该）再走 framed_robot()：那既要求原始源图存在，
    又会把成品重新框选 + 重采样。
    """
    if not ROBOT.exists():
        raise SystemExit(f"找不到 {ROBOT}（机器人母版），无法生成闪屏图。")
    SPLASH.mkdir(parents=True, exist_ok=True)
    robot = Image.open(ROBOT).convert("RGBA")
    robot.resize((SPLASH_PX, SPLASH_PX), Image.Resampling.LANCZOS).save(
        SPLASH / "robot.png", optimize=True
    )
    print(f"  src-tauri/window/robot.png  ← {SPLASH_PX}x{SPLASH_PX}")


def regen_tauri() -> None:
    if not (ROOT / "src-tauri" / "package.json").exists():
        print("  ! 未找到 src-tauri/package.json，跳过 tauri icon")
        return
    # bun 在 Windows 上是 .CMD shim，Python 子进程需 shell=True 才能解析。
    subprocess.run(
        "bun x tauri icon \"%s\" -o \"%s\"" % (ROBOT, TAURI_ICONS),
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
        help="只重生成桌面闪屏图（不依赖原始源图）",
    )
    args = parser.parse_args()

    if args.splash_only:
        regen_splash()
        return

    # 以下都需要原始源图。源图缺失时 framed_robot() 会在任何写入之前中止，
    # 避免留下半更新的图标集。
    robot = framed_robot()
    ROBOT.parent.mkdir(parents=True, exist_ok=True)
    robot.save(ROBOT)
    print(f"images/codex_icon.png  ← {ROBOT.name} {SQUARE}x{SQUARE}")

    regen_masters(robot)
    regen_brand(robot)
    regen_splash()

    if args.tauri:
        regen_tauri()
    else:
        print("  （跳过 tauri icon，如需请加 --tauri）")


if __name__ == "__main__":
    main()
