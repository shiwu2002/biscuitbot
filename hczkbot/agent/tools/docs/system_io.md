# system_io

在宿主操作系统上执行系统级 IO：模拟键盘/鼠标输入、读写剪贴板、列出 USB 设备、读写串口。

## 何时使用

需要让智能体直接操作本机硬件或系统输入时使用，例如 GUI 自动化（按键、点击、输入文本）、读取/设置剪贴板内容、枚举 USB 设备、与串口外设通信。该工具默认关闭，需要在配置中显式开启 `tools.system_io.enable`，因为它直接操作宿主 OS，具有真实世界的副作用。

支持的 action：

| action | 类别 | 说明 |
|--------|------|------|
| clipboard_read | 读 | 读取系统剪贴板文本 |
| clipboard_write | 写 | 写入系统剪贴板文本 |
| key_tap | 写 | 模拟按键（单键或组合键，如 `cmd+c`） |
| key_type | 写 | 模拟键盘输入一整段文本 |
| mouse_move | 写 | 移动鼠标到指定坐标 |
| mouse_click | 写 | 在指定坐标点击鼠标（左/右/中） |
| mouse_scroll | 写 | 鼠标滚轮滚动 |
| usb_list | 读 | 列出 USB 设备 |
| serial_list | 读 | 列出可用串口 |
| serial_write | 写 | 向串口写入数据 |
| serial_read | 写 | 从串口读取数据（会占用串口） |

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 要执行的系统 IO 动作（见上表） |
| text | string | 否 | - | clipboard_write / key_type / serial_write 的文本载荷 |
| keys | string | 否 | - | key_tap 的按键序列，用 `+` 连接组合键，如 `cmd+c`、`ctrl+shift+esc`、`enter`、`f5` |
| x | integer | 否 | - | mouse_move / mouse_click 的屏幕 X 坐标 |
| y | integer | 否 | - | mouse_move / mouse_click 的屏幕 Y 坐标 |
| button | string | 否 | left | mouse_click 的按键：left / right / middle |
| scroll_dx | integer | 否 | - | mouse_scroll 的水平滚动量 |
| scroll_dy | integer | 否 | - | mouse_scroll 的垂直滚动量（正=向下） |
| port | string | 否 | - | serial_write / serial_read 的串口设备名，如 `/dev/ttyUSB0` 或 `COM3` |
| baudrate | integer | 否 | 9600 | 串口波特率 |
| bytes_to_read | integer | 否 | 128 | serial_read 读取的字节数 |
| timeout_ms | integer | 否 | 3000 | 单次动作超时（毫秒，100-30000） |

## 调用示例

```
system_io(action="clipboard_read")
system_io(action="clipboard_write", text="hello world")
system_io(action="key_tap", keys="cmd+c")
system_io(action="key_tap", keys="ctrl+shift+esc")
system_io(action="key_type", text="Hello")
system_io(action="mouse_move", x=500, y=300)
system_io(action="mouse_click", x=500, y=300, button="left")
system_io(action="mouse_scroll", scroll_dy=-3)
system_io(action="usb_list")
system_io(action="serial_list")
system_io(action="serial_write", port="/dev/ttyUSB0", baudrate=115200, text="AT\r\n")
system_io(action="serial_read", port="/dev/ttyUSB0", baudrate=115200, bytes_to_read=64)
```

## 平台依赖

不同 action 在不同平台的后端选择：

| 能力 | macOS | Linux | Windows |
|------|-------|-------|---------|
| 剪贴板 | `pbpaste` / `pbcopy`（内置） | `xclip` 或 `xsel` | `powershell Get-Clipboard` / `Set-Clipboard` |
| 键盘模拟 | 优先 `pynput`，回退 `osascript` | 优先 `pynput`，回退 `xdotool` | 需要 `pynput` |
| 鼠标模拟 | 需要 `pynput` | 优先 `pynput`，回退 `xdotool` | 需要 `pynput` |
| USB 列表 | `system_profiler`（内置） | `lsusb` | `Get-PnpDevice` |
| 串口列表 | `pyserial` 或扫描 `/dev/tty.*` | `pyserial` 或扫描 `/dev/ttyUSB*` | `pyserial` 或扫描 `COM*` |
| 串口读写 | 需要 `pyserial` | 需要 `pyserial` | 需要 `pyserial` |

`pynput` 和 `pyserial` 已随 hczkbot 默认安装，无需额外安装可选依赖。

## 注意事项

- **默认关闭**：必须在配置中设置 `tools.system_io.enable = true` 才会注册。该工具直接操作宿主 OS，具有真实世界的副作用，请谨慎授权。
- **白名单收窄**：可通过 `tools.system_io.allow_actions` 限制只允许部分 action，例如只开启读类操作 `["clipboard_read", "usb_list", "serial_list"]`。
- **独占执行**：所有写类 action（按键、鼠标、剪贴板写入、串口）不会与其他工具并行，避免输入混乱。
- **权限要求**：macOS 上键盘/鼠标模拟与屏幕录制需要「辅助功能」与「屏幕录制」权限（系统设置 → 隐私与安全性）；Linux 上 X11 需要 `DISPLAY` 与可访问的 X server。
- **未知 action** 会被拒绝；不在 `allow_actions` 白名单内的 action 也会被拒绝。
- **超时**：每个 action 默认 3 秒超时，可通过 `timeout_ms` 调整（最大 30 秒）。
- **输出截断**：USB 列表等输出较长时会自动截断到 8000 字符。
- **安全提示**：模拟输入与剪贴板操作可能触发系统弹窗或影响当前焦点应用，调用前请确认目标场景。
