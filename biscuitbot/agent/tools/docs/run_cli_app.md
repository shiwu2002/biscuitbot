 # run_cli_app

运行已注册的 CLI 应用。

## 何时使用

需要调用预定义的命令行工具或应用时使用。区别于 exec 的通用命令执行，专用于已注册的 CLI 应用，参数更规范，调用更安全。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| name | string | 是 | - | CLI 应用注册名称（如 gimp, safari, obsidian）。不要用于普通系统 CLI（如 git, gh, python, npm, brew），未注册名称会被拒绝 |
| args | array | 否 | - | 传递给应用的命令行参数数组（不包含入口点） |
| json | boolean | 否 | false | 是否在 CLI 支持时自动添加 --json |
| working_dir | string | 否 | - | 工作目录 |
| timeout | integer | 否 | - | 超时秒数（1-600） |

## 调用示例

```
run_cli_app(name="gimp", args=["-i", "-b", "(gimp-image-flatten)"])
run_cli_app(name="pandoc", args=["input.md", "-o", "output.pdf"], working_dir="/tmp")
run_cli_app(name="youtube-dl", args=["https://example.com/video", "--extract-audio"], json=true)
```

## 注意事项

- name 必须为已注册的 CLI 应用
- args 为字符串数组，每个元素是一个命令行参数
- json=true 适用于支持结构化输出的应用
- 交互式应用可能无法正常运行
