---
name: douyin-publish
tier: user
description: 自动发布抖音图文/视频作品：按当前平台自动路由——Windows 走 Playwright MCP / Edge CDP 接管（备选 Python Playwright），macOS 走 Playwright + Chromium 反检测脚本（playwright-stealth，含短信验证处理）。
metadata: {"xianaibot":{"emoji":"🎵"}}
---

# 抖音发布（跨平台）

本技能用于自动发布抖音作品（图文或视频），内部按操作系统路由到对应流程：

- **Windows** → 方式 A：Playwright MCP 工具调用（优先）；方式 B：Python Playwright 脚本（备选）
- **macOS** → Playwright + Chromium 持久化浏览器脚本（反检测增强版）

用 `exec` 判断当前平台（Windows 有 `powershell`，macOS 有 `python3`）后走对应章节。
本技能只在 Windows 与 macOS 可用——其他系统上没有可用的发布流程，不要尝试。

---

## Windows 流程

### 前置条件

- Windows 10/11 + PowerShell
- Microsoft Edge 浏览器（用于 CDP 接管）
- 推荐：Node.js + npx（用于 Playwright MCP）
- 备选：Python + `pip install playwright` + `playwright install msedge`

### 方式 A（优先）：Playwright MCP 工具调用

当 `config.tools.mcpServers.playwright` 已配置时，直接调用 MCP 工具操控浏览器。

#### 配置示例（config.json）

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

#### 启动 Edge 并开启 CDP

```powershell
# 关闭所有 Edge 实例后执行
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
& $edge --remote-debugging-port=9222 --user-data-dir="$env:TEMP\edge-douyin"
```

然后在 Edge 中手动登录 https://creator.douyin.com 一次，登录态会持久化到 user-data-dir。

#### 发布流程

1. 调用 MCP `browser_navigate` 打开 https://creator.douyin.com/creator-micro/content/upload
2. 调用 `browser_file_upload` 上传图片/视频文件
3. 调用 `browser_type` 填写标题、话题
4. 调用 `browser_click` 点击「发布」按钮
5. 调用 `browser_wait_for` 等待发布完成提示

### 方式 B（备选）：Python Playwright 脚本

当 MCP 未配置或不可用时，使用 Python Playwright 脚本完成相同流程。

#### 安装依赖

```powershell
pip install playwright
playwright install msedge
```

#### 启动 Edge 并开启 CDP

同方式 A 的 PowerShell 命令。

#### 参考脚本模板

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

---

## macOS 流程（Playwright + Chromium 反检测）

通过 Playwright 驱动 Chromium 持久化浏览器，自动完成抖音创作者中心图文发布全流程。

### 前置条件

1. **Python 环境**：需要 `playwright` + `playwright-stealth`，Chromium 浏览器已安装
2. **浏览器 Profile**：`~/.xianaibot/workspace/douyin_profile/` 中保存登录态（首次需手动扫码登录）
3. **图片**：1-N 张本地图片，格式 jpg/png/webp

### 使用方法

```bash
python3 "<技能目录>/douyin_playwright.py" <图片1> <图片2> ... -t "标题文案"
```

#### 参数

| 参数 | 说明 |
|------|------|
| `images` | 图片路径（必填，1-N 个） |
| `-t / --title` | 标题文案（默认 "测试作品"） |
| `-s / --sms-code` | 短信验证码（可选，遇到二次验证时自动填入） |
| `--headless` | 无头模式（不推荐，验证码无法手动处理） |

#### 示例

```bash
python3 "<技能目录>/douyin_playwright.py" img1.png img2.png img3.png \
  -t "不会写代码也能定制自己的 AI 智能体？保姆级教程来了！"
```

### 发布流程（5 步）

1. **打开上传页** → `creator.douyin.com/creator-micro/content/upload`
2. **切换图文 Tab + 上传图片** → 点击「发布图文」，选择图片文件
3. **填写标题** → 在 contenteditable 区域填入标题
4. **点击发布** → 找到并点击「发布」按钮
5. **验证处理** → 检测短信二次验证弹窗

### 短信验证

- 如果提供了 `--sms-code`：自动填入验证码并确认
- 如果未提供验证码：脚本等待 120 秒，请在浏览器中手动输入

**手动验证步骤：**
1. 切换到弹出的 Chromium 窗口
2. 点击「获取验证码」，手机号尾号 **27**
3. 输入收到的验证码并确认
4. 脚本检测到页面跳转到管理页后自动结束

### 反检测措施

- `playwright-stealth` 自动化指纹隐藏（navigator/webgl/chrome runtime）
- 模拟 Apple M4 GPU 渲染器
- 随机化 viewport 尺寸
- 随机鼠标移动 + 操作延迟
- 模拟北京地理定位
- 中文优先的 Accept-Language

### 常见问题

| 问题 | 解决 |
|------|------|
| 找不到「发布图文」Tab | 确认已登录；手动在浏览器中登录后重试 |
| 无图片 input | 页面停留在视频 Tab，需确认切换成功 |
| 验证码超时 | 浏览器窗口可能被遮挡，手动切换到窗口操作 |
| 标题填不进去 | contenteditable 元素变化，检查截图 `/tmp/douyin_v15_*.png` |

### 脚本位置

`douyin_playwright.py` 与本 SKILL.md 同目录（本技能自带的内置脚本，**不在**工作区里）。
用技能索引里本技能的目录路径拼出绝对路径再调用，不要假设当前 cwd，也不要去找
`~/.xianaibot/workspace/skills/douyin-publish/`——那里没有这个文件。

---

## 通用注意事项

- 发布频率不要过高，建议每次间隔至少 5 分钟，避免触发风控
- 首次使用前务必在浏览器中完成登录并保持会话
- 如遇验证码，需手动完成后再重试
- 视频文件建议 < 4GB，图片建议 < 20MB
