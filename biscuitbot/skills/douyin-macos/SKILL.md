---
name: douyin-macos
tier: user
description: 在 macOS 上通过 Safari + AppleScript 发布抖音图文/视频作品。
metadata: {"biscuitbot":{"requires":{"bins":["osascript"]}}}
---

# 抖音发布（macOS）

本技能用于在 macOS 平台上自动发布抖音作品（图文或视频），使用 Safari + AppleScript + mkcert 本地 HTTPS 方案。

## 前置条件

- macOS 12+
- Safari 浏览器
- `mkcert`（用于本地 HTTPS 代理）
- 已在 Safari 中登录 https://creator.douyin.com

## 方式 A：Safari + AppleScript 直接操控

### 启动 Safari 并打开抖音创作者中心

```bash
osascript -e 'tell application "Safari"
  activate
  open location "https://creator.douyin.com/creator-micro/content/upload"
end tell'
```

### 上传文件并填写信息

由于 Safari 的 AppleScript 支持有限，复杂文件上传建议配合 JavaScript 注入：

```applescript
tell application "Safari"
  -- 等待页面加载
  repeat until (do JavaScript "document.readyState" in front document) is "complete"
    delay 0.5
  end repeat

  -- 注入标题
  do JavaScript "document.querySelector('[contenteditable=\"true\"]').innerText = '测试作品 #自动化'" in front document

  -- 触发发布按钮点击
  do JavaScript "document.querySelector('button.publish-btn').click()" in front document
end tell
```

## 方式 B：mkcert 本地 HTTPS 代理 + Playwright

适用于需要更精细控制上传流程的场景。

### 1. 安装 mkcert 并生成本地证书

```bash
brew install mkcert nss
mkcert -install
mkcert localhost 127.0.0.1
```

### 2. 启动本地 HTTPS 代理

```bash
# 安装依赖
pip install mitmproxy

# 启动代理（使用 mkcert 生成的证书）
mitmdump --listen-port 8080 \
  --certs localhost.pem \
  --set confdir=~/.mitmproxy
```

### 3. 配置 Safari 使用本地代理

在「系统设置 → 网络 → Wi-Fi → 代理」中设置：
- Web 代理 (HTTP): 127.0.0.1:8080
- 安全 Web 代理 (HTTPS): 127.0.0.1:8080

### 4. 使用 Playwright 通过代理连接

```python
from playwright.sync_api import sync_playwright

def publish_douyin_macos(media_paths, title, tags):
    with sync_playwright() as p:
        browser = p.webkit.launch(
            headless=False,
            proxy={"server": "http://127.0.0.1:8080"},
            args=["--ignore-certificate-errors"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://creator.douyin.com/creator-micro/content/upload")

        # 上传文件
        page.locator('input[type="file"]').set_input_files(media_paths)
        page.wait_for_selector(".progress-bar", state="hidden", timeout=120000)

        # 填写标题
        title_text = title + "".join(f" #{t}" for t in tags)
        page.locator('[contenteditable="true"]').first.fill(title_text)

        # 发布
        page.get_by_role("button", name="发布").click()
        page.wait_for_url("**/content/manage**", timeout=60000)
        print("发布成功")
        browser.close()
```

## 注意事项

- macOS Safari 的 AppleScript 自动化能力有限，复杂场景建议用方式 B
- 首次使用 mkcert 需要执行 `mkcert -install` 安装根证书
- 使用代理方式时，抖音可能检测到代理 IP，建议仅在本地使用
- 发布频率不要过高，建议每次间隔至少 5 分钟
- 如遇验证码，需手动完成后再重试
