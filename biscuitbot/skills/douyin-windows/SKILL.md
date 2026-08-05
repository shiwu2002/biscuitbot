---
name: douyin-windows
tier: user
description: 在 Windows 上通过浏览器自动化发布抖音图文/视频作品（Playwright MCP 优先，Python Playwright 备选）。
metadata: {"biscuitbot":{"requires":{"bins":["powershell"]}}}
---

# 抖音发布（Windows）

本技能用于在 Windows 平台上自动发布抖音作品（图文或视频）。支持两种执行方式，按优先级自动选择。

## 前置条件

- Windows 10/11 + PowerShell
- Microsoft Edge 浏览器（用于 CDP 接管）
- 推荐：Node.js + npx（用于 Playwright MCP）
- 备选：Python + `pip install playwright` + `playwright install msedge`

## 方式 A（优先）：Playwright MCP 工具调用

当 `config.tools.mcpServers.playwright` 已配置时，直接调用 MCP 工具操控浏览器。

### 配置示例（config.json）

```json
{
  "tools": {
    "mcpServers": {
      "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--cdp-endpoint", "http://127.0.0.1:9222"],
        "enabledTools": ["*"],
        "toolTimeout": 120
      }
    }
  }
}
```

### 启动 Edge 并开启 CDP

```powershell
# 关闭所有 Edge 实例后执行
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
& $edge --remote-debugging-port=9222 --user-data-dir="$env:TEMP\edge-douyin"
```

然后在 Edge 中手动登录 https://creator.douyin.com 一次，登录态会持久化到 user-data-dir。

### 发布流程

1. 调用 MCP `browser_navigate` 打开 https://creator.douyin.com/creator-micro/content/upload
2. 调用 `browser_file_upload` 上传图片/视频文件
3. 调用 `browser_type` 填写标题、话题
4. 调用 `browser_click` 点击「发布」按钮
5. 调用 `browser_wait_for` 等待发布完成提示

## 方式 B（备选）：Python Playwright 脚本

当 MCP 未配置或不可用时，使用 Python Playwright 脚本完成相同流程。

### 安装依赖

```powershell
pip install playwright
playwright install msedge
```

### 启动 Edge 并开启 CDP

同方式 A 的 PowerShell 命令。

### 参考脚本模板

```python
from playwright.sync_api import sync_playwright

CDP_ENDPOINT = "http://127.0.0.1:9222"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

def publish_douyin(media_paths: list[str], title: str, tags: list[str]) -> None:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(UPLOAD_URL, wait_until="networkidle")

        # 上传文件
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(media_paths)

        # 等待上传完成
        page.wait_for_selector(".progress-bar", state="hidden", timeout=120000)

        # 填写标题和话题
        title_box = page.locator('[contenteditable="true"]').first
        title_box.fill("")
        title_text = title
        for tag in tags:
            title_text += f" #{tag}"
        title_box.type(title_text)

        # 点击发布
        publish_btn = page.get_by_role("button", name="发布")
        publish_btn.click()

        # 等待发布成功
        page.wait_for_url("**/content/manage**", timeout=60000)
        print("发布成功")

if __name__ == "__main__":
    publish_douyin(
        media_paths=["C:\\path\\to\\image.jpg"],
        title="测试作品",
        tags=["测试", "自动化"],
    )
```

## 注意事项

- 发布频率不要过高，建议每次间隔至少 5 分钟，避免触发风控
- 首次使用前务必在 Edge 中完成登录并保持会话
- 如遇验证码，需手动完成后再重试
- 视频文件建议 < 4GB，图片建议 < 20MB
