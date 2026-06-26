# exec

执行 shell 命令并返回输出结果。

## 何时使用

需要在终端中执行系统命令时使用，例如运行脚本、安装依赖、构建项目或查看进程信息。支持指定工作目录、超时时间和 shell 类型，适合大多数需要与系统交互的场景。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| command | string | 是 | - | 要执行的 shell 命令 |
| working_dir | string | 否 | 当前目录 | 命令执行的工作目录 |
| timeout | integer | 否 | 60 | 超时时间（秒），最大 600 |
| shell | string | 否 | bash | 使用的 shell 类型（sh/bash/zsh） |
| login | boolean | 否 | false | 是否以登录 shell 方式执行 |
| yield_time_ms | integer | 否 | - | 异步模式下执行前等待时间 |
| max_output_chars | integer | 否 | - | 输出最大字符数限制 |

## 调用示例

```
exec(command="ls -la", working_dir="/tmp", timeout=30, shell="bash")
```

## 注意事项

- timeout 最大值为 600 秒，超过会被截断
- 命令执行失败时会返回非零退出码，请检查返回值
- 输出过大时会被截断，必要时请使用 max_output_chars 控制
- 交互式命令可能阻塞执行，建议使用非交互方式
