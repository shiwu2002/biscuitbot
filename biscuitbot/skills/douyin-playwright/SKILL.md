---
name: douyin-playwright
tier: user
description: 通过 Playwright + Chromium 在 macOS 上自动发布抖音图文作品（反检测增强版）。
metadata: {"biscuitbot":{"emoji":"🎵","requires":{"bins":["python3"],"pkgs":["playwright","playwright-stealth"]}}}
---

# 抖音图文发布 — Playwright 自动化

通过 Playwright 驱动 Chromium 持久化浏览器，自动完成抖音创作者中心图文发布全流程。

## 前置条件

1. **Python 环境**：需要 `playwright` + `playwright-stealth`，Chromium 浏览器已安装
2. **浏览器 Profile**：`~/.biscuitbot/workspace/douyin_profile/` 中保存登录态（首次需手动扫码登录）
3. **图片**：1-N 张本地图片，格式 jpg/png/webp

## 使用方法

```bash
python3 douyin_playwright.py <图片1> <图片2> ... -t "标题文案"
```

### 参数

| 参数 | 说明 |
|------|------|
| `images` | 图片路径（必填，1-N 个） |
| `-t / --title` | 标题文案（默认 "测试作品"） |
| `-s / --sms-code` | 短信验证码（可选，遇到二次验证时自动填入） |
| `--headless` | 无头模式（不推荐，验证码无法手动处理） |

### 示例

```bash
python3 douyin_playwright.py img1.png img2.png img3.png \
  -t "不会写代码也能定制自己的 AI 智能体？保姆级教程来了！"
```

## 发布流程（5 步）

1. **打开上传页** → `creator.douyin.com/creator-micro/content/upload`
2. **切换图文 Tab + 上传图片** → 点击「发布图文」，选择图片文件
3. **填写标题** → 在 contenteditable 区域填入标题
4. **点击发布** → 找到并点击「发布」按钮
5. **验证处理** → 检测短信二次验证弹窗

## 短信验证

- 如果提供了 `--sms-code`：自动填入验证码并确认
- 如果未提供验证码：脚本等待 120 秒，请在浏览器中手动输入

**手动验证步骤：**
1. 切换到弹出的 Chromium 窗口
2. 点击「获取验证码」，手机号尾号 **27**
3. 输入收到的验证码并确认
4. 脚本检测到页面跳转到管理页后自动结束

## 反检测措施

- `playwright-stealth` 自动化指纹隐藏（navigator/webgl/chrome runtime）
- 模拟 Apple M4 GPU 渲染器
- 随机化 viewport 尺寸
- 随机鼠标移动 + 操作延迟
- 模拟北京地理定位
- 中文优先的 Accept-Language

## 常见问题

| 问题 | 解决 |
|------|------|
| 找不到「发布图文」Tab | 确认已登录；手动在浏览器中登录后重试 |
| 无图片 input | 页面停留在视频 Tab，需确认切换成功 |
| 验证码超时 | 浏览器窗口可能被遮挡，手动切换到窗口操作 |
| 标题填不进去 | contenteditable 元素变化，检查截图 `/tmp/douyin_v15_*.png` |

## 脚本位置

`~/.biscuitbot/workspace/skills/douyin-playwright/douyin_playwright.py`
