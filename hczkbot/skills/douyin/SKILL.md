---
name: douyin
description: "通过 Safari + AppleScript 自动化发布抖音图文内容，包括图片上传、标题描述填写和发布结果检查"
tier: user
always: false
---

# Douyin（抖音）发布技能

通过 Safari + AppleScript JavaScript 注入，在已登录的 Safari 会话中自动化发布图文内容。

## 前置条件

- macOS Safari 已登录抖音创作者中心 (creator.douyin.com)
- Safari 开发菜单 → "允许 AppleScript 执行 JavaScript" 已开启
- mkcert 已安装且 CA 证书受信（用于本地 HTTPS 文件服务）

## 整体流程

1. 导航到创作者中心上传页面（图文 tab）
2. 通过本地 HTTPS 服务器注入图片 base64 数据
3. 用 JavaScript DataTransfer API 绕过原生文件对话框上传图片
4. 填写标题、描述、话题标签
5. 滚动到底部，点击「发布」按钮
6. **等待并检查发布结果，避免重复发布**

## 关键步骤详解

### 1. 本地 HTTPS 图片服务

Mac mini 无显示器环境没有辅助功能权限，无法控制原生文件对话框。解决方案：用 mkcert 生成受信证书，启动本地 HTTPS 服务器。

```bash
# 确保 mkcert CA 受信
mkcert -install

# 生成 localhost 证书
mkcert -key-file /tmp/localhost-key.pem -cert-file /tmp/localhost.pem localhost 127.0.0.1 ::1
```

Python HTTPS 服务器（带 CORS 头）：
```python
import http.server, ssl, os
os.chdir('/Users/hehaifeng/Desktop')

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

httpd = http.server.HTTPServer(('127.0.0.1', 8766), MyHandler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/tmp/localhost.pem', '/tmp/localhost-key.pem')
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
```

### 2. 图片 Base64 JS 文件

将图片转为 base64 嵌入 JS 文件：

```python
import base64
img_path = "/path/to/image.png"
with open(img_path, "rb") as f:
    img_data = f.read()
b64 = base64.b64encode(img_data).decode("ascii")

js_code = f'window.__IMG_B64 = "{b64}"; window.__IMG_READY = true;'
with open("/path/to/__img_data.js", "w") as f:
    f.write(js_code)
```

文件放在 HTTPS 服务器的根目录下（如 Desktop）。

### 3. 注入 + 上传（绕过文件对话框）

三种 JavaScript 注入方式的对比：

| 方式 | 可行性 | 原因 |
|------|--------|------|
| 直接传 base64 字符串 | ❌ | AppleScript 字符串长度限制 |
| 本地 HTTP 服务器 | ❌ | 抖音是 HTTPS，浏览器阻止混合内容 |
| 本地 HTTPS + 自签名证书 | ❌ | Safari 不信任自签名证书 |
| **本地 HTTPS + mkcert 受信证书** | ✅ | 证书受信，CORS 头允许跨域 |
| 注入 `<script src="https://127.0.0.1:8766/__img_data.js">` | ✅ | 最佳方案 |

核心上传代码（在 douyin 页面内执行）：

```javascript
// 注入 script 加载 base64
var s = document.createElement("script");
s.src = "https://127.0.0.1:8766/__img_data.js";
s.onload = function() {
    // base64 → Data URL → fetch → Blob → File → DataTransfer
    var dataUrl = "data:image/png;base64," + window.__IMG_B64;
    fetch(dataUrl).then(r => r.blob()).then(blob => {
        var file = new File([blob], "image.png", {type: "image/png"});
        var fi = document.querySelector("input[type=file]");
        var dt = new DataTransfer();
        dt.items.add(file);
        fi.files = dt.files;
        fi.dispatchEvent(new Event("change", {bubbles: true}));
    });
};
document.head.appendChild(s);
```

### 4. 填写标题和描述

**标题：**
```javascript
var titleInput = document.querySelector('input[placeholder*="标题"]');
var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
ns.call(titleInput, "作品标题");
titleInput.dispatchEvent(new Event("input", {bubbles: true}));
```

**描述（contenteditable 富文本编辑器）：**
```javascript
var editor = document.querySelector('[contenteditable="true"].zone-container');
editor.focus();
editor.textContent = "作品描述文字...\n\n#话题标签";
editor.dispatchEvent(new Event("input", {bubbles: true}));
```

### 5. 点击发布

```javascript
// 「发布」按钮在页面底部，需要先滚动
window.scrollTo(0, document.body.scrollHeight);

// 查找并点击
var all = document.querySelectorAll("button");
for (var btn of all) {
    if (btn.textContent.trim() === "发布") {
        btn.scrollIntoViewIfNeeded();
        btn.click();
        break;
    }
}
```

## ⚠️ 关键陷阱：避免重复发布

### 问题

点击「发布」后，抖音可能会：
1. **直接跳到「作品管理」页面** → 作品状态显示「已发布」或「审核中」
2. **停留在上传页面** → 可能有错误提示（如内容违规、网络错误等）

如果状态显示「审核中」，说明发布已经成功提交，只是平台在审核。**此时不应该再次点击发布**，否则会创建重复内容。

### 正确流程

```
点击「发布」
    │
    ├─→ URL 变为 /content/manage?enter_from=publish
    │   └─→ 发布已受理 ✅ → 截图检查状态
    │       ├─ 已发布     → 完成
    │       └─ 审核中     → 等待审核，不做任何操作
    │
    ├─→ URL 仍为 /content/upload
        └─→ 检查是否有错误提示弹窗/Toast
            ├─ 有错误提示 → 根据提示修正后重试
            └─ 无错误提示 → 检查页面是否已完成上传
                └─ 文件仍在上传区 → 重新点击发布
```

### 实现代码

```javascript
// 点击发布后，等待并检查结果
var btn = /* 找到发布按钮 */;
btn.click();

// 等待 3 秒后检查
setTimeout(function() {
    var url = window.location.href;
    if (url.indexOf("/content/manage") !== -1) {
        window.__PUBLISH_RESULT = "submitted";  // 已提交，不再重试
    } else {
        // 仍在上传页，检查错误
        var errors = document.querySelectorAll(".error, .toast-error, [class*=error]");
        if (errors.length > 0) {
            window.__PUBLISH_RESULT = "error: " + errors[0].textContent;
        } else {
            window.__PUBLISH_RESULT = "unknown - check page";
        }
    }
}, 3000);
```

### AppleScript 侧等待检查

```bash
osascript -e '
tell application "Safari"
    set currentURL to URL of current tab of front window
end tell
'

# 如果 URL 包含 /manage → 发布已受理
# 如果 URL 仍为 /upload → 检查页面 DOM 中的错误提示
```

## 已知限制

1. **无辅助功能权限** → 无法控制原生 macOS 对话框（文件选择器、系统弹窗等）
2. **需 Safari 保持登录** → Cookie 过期后需要重新手动登录
3. **图片通过 base64 注入** → 超大图片可能有性能问题（已在 ~1.3MB PNG 上验证可行）
4. **页面 DOM 可能变化** → 选择器需要根据抖音更新调整

## 适用文件位置

- 图片文件：通常在 `/Users/hehaifeng/Desktop/` 或 `media/feishu/`
- HTTPS 服务端口：`8766`
- JS 数据文件：`/Users/hehaifeng/Desktop/__img_data.js`
- mkcert 证书：`/tmp/localhost.pem` 和 `/tmp/localhost-key.pem`
