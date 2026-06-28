 # exec

执行 shell 命令并返回输出。

## 何时使用

需要运行测试、构建、包管理、git 命令等进程时使用。适合自动化脚本执行和系统操作。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| command | string | 是 | - | 要执行的 shell 命令 |
| cmd | string | 否 | - | command 的兼容别名 |
| working_dir | string | 否 | - | 命令的工作目录 |
| workdir | string | 否 | - | working_dir 的兼容别名 |
| timeout | integer | 否 | 60 | 超时秒数（最大 600） |
| shell | string | 否 | - | 指定 shell（如 sh/bash/zsh），Unix 下支持 |
| login | boolean | 否 | true | bash/zsh 是否使用登录 shell 语义 |
| yield_time_ms | integer | 否 | - | 在返回前等待的毫秒数。设置后，仍在运行的命令会返回 session_id，可通过 write_stdin 继续交互 |
| max_output_chars | integer | 否 | 10000 | 返回的最大输出字符数（上限 50000）。仅在 yield_time_ms 设置时有效 |
| max_output_tokens | integer | 否 | - | max_output_chars 的兼容别名 |

## 调用示例

```
exec(command="ls /home")
exec(command="npm test")
exec(command="python -c 'input()'", yield_time_ms=500)
```

## 注意事项

- 输出上限为 10000 字符，超出会截断
- timeout 默认 60 秒，长时间任务建议加大
- yield_time_ms 用于交互式/长时间命令，返回 session_id 供 write_stdin 使用
- 危险命令会被拦截
