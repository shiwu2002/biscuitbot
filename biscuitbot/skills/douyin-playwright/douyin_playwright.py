#!/usr/bin/env python3
"""
抖音创作者中心 - Playwright + Chromium v15 (反检测增强版)
新增：
- playwright-stealth 反指纹检测（navigator/webgl/chrome runtime 等）
- 更多浏览器反检测启动参数
- 随机化 viewport
- 模拟人类操作的随机延迟
- 鼠标移动模拟
"""
import sys
import time
import random
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
USER_DATA_DIR = str(Path.home() / ".biscuitbot/workspace/douyin_profile")

# 反检测 Stealth 配置（针对中文/macOS 优化）
STEALTH = Stealth(
    navigator_languages_override=("zh-CN", "zh", "en-US", "en"),
    navigator_platform_override="MacIntel",
    navigator_vendor_override="Google Inc.",
    chrome_runtime=True,
    chrome_app=True,
    chrome_csi=True,
    chrome_load_times=True,
    hairline=True,
    iframe_content_window=True,
    media_codecs=True,
    navigator_hardware_concurrency=True,
    navigator_languages=True,
    navigator_permissions=True,
    navigator_platform=True,
    navigator_plugins=True,
    navigator_user_agent=True,
    navigator_user_agent_data=True,
    navigator_vendor=True,
    navigator_webdriver=True,
    error_prototype=True,
    sec_ch_ua=True,
    webgl_vendor=True,
    # 模拟 Apple M4 GPU
    webgl_renderer_override="Apple M4",
    webgl_vendor_override="Apple Inc.",
)

# 更多反检测启动参数
ANTI_DETECT_ARGS = [
    # 基础反检测
    "--disable-blink-features=AutomationControlled",
    "--disable-features=AutomationControlled",
    # 隐藏自动化标识
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # 模拟真实浏览器环境
    "--disable-automation",
    "--disable-infobars",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-client-side-phishing-detection",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-web-security",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    # 语言/区域
    "--lang=zh-CN",
    "--accept-lang=zh-CN,zh;q=0.9,en;q=0.8",
]

def wprint(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def rand_sleep(base, spread=0.5):
    """带随机抖动的等待"""
    t = base * (1 + random.uniform(-spread, spread))
    time.sleep(max(0.1, t))

def human_delay(action):
    """模拟人类操作延迟"""
    delays = {
        "type": 80,      # 打字延迟 ms（playwright 的 delay）
        "click": 0.3,    # 点击后停顿
        "navigate": 0.5, # 页面跳转后停顿
        "think": 1.5,    # "思考"时间
    }
    return delays.get(action, 0.5)

def screenshot(page, name):
    path = f"/tmp/douyin_v15_{name}.png"
    page.screenshot(path=path)
    wprint(f"📸 {path}")

def move_mouse_randomly(page):
    """随机移动鼠标，模拟人类行为"""
    try:
        vw = page.viewport_size
        if vw:
            x = random.randint(100, vw["width"] - 100)
            y = random.randint(100, vw["height"] - 100)
            page.mouse.move(x, y, steps=random.randint(5, 15))
    except:
        pass

def is_confirm_page(page):
    return "content/post/image" in page.url or "content/post/video" in page.url

def is_manage_page(page):
    return "content/manage" in page.url

def handle_sms_verify(page):
    """检测 SMS 验证弹窗"""
    selectors = [
        "#uc-second-verify",
        '[class*="second-verify"]',
        '[class*="verify"]:has(input[placeholder*="验证码"])',
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                wprint(f"📱 检测到验证弹窗: {sel}")
                return True
        except:
            continue
    for check in ["短信验证", "安全验证", "二次验证"]:
        try:
            v = page.locator(f'text={check}').first
            if v.is_visible(timeout=500):
                wprint(f"📱 检测到: {check}")
                return True
        except:
            continue
    return False

def do_sms_verify(page, sms_code: str):
    """执行短信验证"""
    wprint("📱 SMS 验证流程...")
    screenshot(page, "sms_01")

    # 点击发送验证码
    for txt in ["获取验证码", "发送验证码", "获取短信验证码"]:
        try:
            btn = page.locator(f'button:has-text("{txt}")').first
            if btn.is_visible(timeout=2000):
                wprint(f"📤 点击「{txt}」")
                btn.click()
                rand_sleep(3)
                break
        except:
            continue
    else:
        for txt in ["获取验证码", "发送验证码"]:
            try:
                el = page.locator(f'text="{txt}"').first
                if el.is_visible(timeout=1000):
                    wprint(f"📤 点击「{txt}」")
                    el.click()
                    rand_sleep(3)
                    break
            except:
                continue

    # 填入验证码
    wprint(f"📝 填入验证码: {sms_code}")
    try:
        code_input = page.locator('input[placeholder*="验证码"]').first
        code_input.wait_for(state="visible", timeout=5000)
        code_input.click()
        rand_sleep(0.3)
        code_input.fill("")
        rand_sleep(0.2)
        code_input.type(sms_code, delay=human_delay("type"))
        wprint("✅ 验证码已填入")
    except:
        wprint("⚠️ 填入失败，尝试 fallback")
        for inp in page.locator("input:visible").all():
            try:
                ph = (inp.get_attribute("placeholder") or "").lower()
                if "验证" in ph or "码" in ph:
                    inp.fill(sms_code)
                    wprint("✅ fallback 成功")
                    break
            except:
                continue

    screenshot(page, "sms_02_filled")

    # 点击确认
    for txt in ["确认", "确定", "提交", "验证"]:
        try:
            btn = page.locator(f'button:has-text("{txt}")').first
            if btn.is_visible(timeout=2000):
                wprint(f"🚀 点击「{txt}」")
                btn.click()
                rand_sleep(3)
                break
        except:
            continue
    else:
        wprint("⚠️ 按 Enter")
        page.keyboard.press("Enter")
        rand_sleep(3)

    screenshot(page, "sms_03_confirmed")

    try:
        page.wait_for_url(
            lambda url: "content/manage" in url or "content/upload" in url,
            timeout=30000
        )
        wprint(f"✅ 验证后跳转: {page.url}")
    except PWTimeout:
        if not handle_sms_verify(page):
            wprint("✅ 验证弹窗已消失")


def publish_images(image_paths: list[str], title: str, sms_code: str = None,
                   headless: bool = False):
    abs_paths = [str(Path(p).resolve()) for p in image_paths]
    for p in abs_paths:
        if not Path(p).exists():
            raise FileNotFoundError(f"图片不存在: {p}")
        wprint(f"📷 {Path(p).name}")
    wprint(f"📝 标题: {title}")
    if sms_code:
        wprint(f"📱 验证码: {sms_code}")

    with sync_playwright() as p:
        # 可选：hook playwright context 自动对所有 page 应用 stealth
        # 这里手动对 page 应用，更精确

        vw = {"width": 1440 + random.randint(-40, 40),
              "height": 900 + random.randint(-20, 20)}

        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=headless,
            args=ANTI_DETECT_ARGS,
            viewport=vw,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            # 模拟真实 User-Agent（可选，stealth 会自动处理）
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            # 设备像素比
            device_scale_factor=2,
            is_mobile=False,
            has_touch=False,
            # 权限
            permissions=["geolocation"],
            geolocation={"latitude": 39.9042, "longitude": 116.4074},  # 北京
            # 颜色方案
            color_scheme="light",
        )

        page = context.new_page()

        # 🛡️ 应用 playwright-stealth
        STEALTH.apply_stealth_sync(page)
        wprint("🛡️ Stealth 已应用")

        # 额外反检测脚本
        page.add_init_script("""
            // 覆盖 webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            // 模拟 chrome 对象
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            // 覆盖 permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );
            // 覆盖 plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            // 覆盖 languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en-US', 'en']
            });
        """)

        page.on("dialog", lambda dialog: dialog.dismiss())

        # ====== Step 1: Open ======
        wprint("🌐 Step 1/5: 打开上传页")
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
        rand_sleep(8, 0.2)
        wprint(f"📍 {page.url}")
        screenshot(page, "01_open")

        # 模拟人类滚动
        move_mouse_randomly(page)

        if "login" in page.url or "passport" in page.url:
            wprint("🔐 请登录...")
            page.wait_for_url("**/creator.douyin.com/**", timeout=300000)
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            rand_sleep(8, 0.2)

        # ====== Step 2: Tab + Upload ======
        wprint("🖱️ Step 2/5: 切换发布图文 + 上传图片")
        try:
            tab = page.locator("text=发布图文").first
            move_mouse_randomly(page)
            tab.click(timeout=5000)
            wprint("   ✅ 已切换")
        except:
            wprint("   ⚠️ 切换可能失败")
        rand_sleep(4)

        # 找图片 input
        all_inputs = page.locator('input[type="file"]')
        img_input = None
        for i in range(all_inputs.count()):
            accept = (all_inputs.nth(i).get_attribute("accept") or "").lower()
            if "image" in accept:
                img_input = all_inputs.nth(i)
                break
        if not img_input:
            wprint("❌ 无图片 input")
            context.close()
            return
        img_input.set_input_files(abs_paths)
        wprint(f"   ✅ {len(abs_paths)} 张已选")

        # 等待上传完成 + 页面跳转
        for i in range(30):
            rand_sleep(2)
            if is_confirm_page(page):
                wprint(f"   ✅ 自动跳转到确认页 ({(i+1)*2}s)")
                break
            try:
                if page.locator('button:has-text("继续添加")').first.is_visible(timeout=500):
                    wprint(f"   ✅ 上传完成 ({(i+1)*2}s)")
                    for j in range(5):
                        rand_sleep(2)
                        if is_confirm_page(page):
                            wprint("      → 跳转确认页")
                            break
                    break
            except:
                pass

        wprint(f"📍 {page.url}")
        screenshot(page, "02_uploaded")

        # ====== Step 3: Title ======
        wprint("✍️ Step 3/5: 填写标题")
        try:
            ce = page.locator('[contenteditable="true"]').first
            ce.wait_for(state="visible", timeout=10000)
            ce.click()
            rand_sleep(1)
            ce.press("Meta+a")
            rand_sleep(0.3)
            ce.press("Backspace")
            rand_sleep(0.3)
            ce.type(title, delay=human_delay("type"))
            rand_sleep(1)
            wprint(f"   ✅ {title}")
        except Exception as e:
            wprint(f"   ⚠️ {e}")

        screenshot(page, "03_titled")

        # ====== Step 4: Publish ======
        wprint("🚀 Step 4/5: 点击发布")
        found = False
        rand_sleep(2)
        # 先随机移动鼠标
        move_mouse_randomly(page)
        rand_sleep(human_delay("think"))

        for btn in page.locator("button").all():
            try:
                if btn.inner_text(timeout=500).strip() == "发布" and \
                   btn.is_enabled(timeout=500):
                    btn.click()
                    wprint("   ✅ 已点击发布")
                    rand_sleep(5)
                    found = True
                    break
            except:
                pass
        if not found:
            try:
                page.locator('button:has-text("发布")').first.click(timeout=5000)
                wprint("   ✅ 已点击发布 (fallback)")
                rand_sleep(5)
            except:
                wprint("   ❌ 未找到发布按钮")
                context.close()
                return

        screenshot(page, "04_publish_clicked")

        # ====== Step 5: SMS Verify ======
        wprint("📱 Step 5/5: 检测验证弹窗")
        for attempt in range(15):
            rand_sleep(2)

            if is_manage_page(page):
                wprint("   ✅ 已进入管理页（无需验证）")
                break

            if handle_sms_verify(page):
                if sms_code:
                    do_sms_verify(page, sms_code)
                else:
                    wprint("   ⚠️ 需要验证码但未提供！")
                    wprint("   📩 请在浏览器中手动输入验证码并确认")
                    wprint("   📞 手机号 193******27")
                    wprint("   ⏳ 等待 120 秒...")
                    try:
                        page.wait_for_url(
                            lambda url: "content/manage" in url or
                                        "content/upload" in url,
                            timeout=120000
                        )
                        wprint("   ✅ 页面已跳转")
                    except PWTimeout:
                        wprint("   ⚠️ 超时")
                break

            if not is_confirm_page(page):
                wprint(f"   📍 页面变化: {page.url}")
                break

        # 再次检查
        if handle_sms_verify(page):
            if sms_code:
                do_sms_verify(page, sms_code)
            else:
                wprint("   ⚠️ 验证弹窗仍在，需手动处理")

        # ====== Final ======
        rand_sleep(3)
        wprint(f"📍 最终: {page.url}")
        screenshot(page, "99_final")
        wprint("🏁 流程结束!")
        context.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="抖音图文发布 v15 (反检测增强)")
    parser.add_argument("images", nargs="+", help="图片路径")
    parser.add_argument("--title", "-t", default="测试作品", help="标题")
    parser.add_argument("--sms-code", "-s", default=None, help="短信验证码")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    args = parser.parse_args()
    publish_images(args.images, args.title, sms_code=args.sms_code,
                   headless=args.headless)
