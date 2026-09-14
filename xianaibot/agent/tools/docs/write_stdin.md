# write_stdin

向正在运行的 exec 会话写入标准输入，或控制其生命周期。

## 何时使用

当 exec 命令通过 yield_time_ms 参数返回会话 ID 后，使用 write_stdin 向该会话发送输入、等待特定输出、关闭 stdin、终止进程或轮询输出。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| session_id | string | 是 | - | 要操作的执行会话 ID |
| chars | string | 否 | - | 要写入进程 stdin 的字符 |
| close_stdin | boolean | 否 | false | 写入 chars 后关闭 stdin（适用于等待 EOF 的命令） |
| terminate | boolean | 否 | false | 终止进程（发送 SIGTERM 后等待再 SIGKILL） |
| yield_time_ms | integer | 否 | 1000 | 写入后等待的毫秒数（0–30000），等待期间可继续用 write_stdin 轮询 |
| wait_for | string | 否 | - | 在输出中等待出现的子串，匹配后返回 |
| wait_timeout_ms | integer | 否 | 10000 | wait_for 的超时毫秒数（0–120000） |
| max_output_chars | integer | 否 | 10000 | 返回的最大输出字符数（1000–50000） |
| max_output_tokens | integer | 否 | - | max_output_chars 的兼容别名 |

## 调用示例

```
write_stdin(session_id="abc123", chars="y\n")
write_stdin(session_id="abc123", close_stdin=true)
write_stdin(session_id="abc123", wait_for="Done", yield_time_ms=2000)
write_stdin(session_id="abc123", terminate=true)
```

## 注意事项

- session_id 必须为 exec 返回的有效会话 ID
- chars 会被直接写入进程的标准输入
- 需要先通过 exec(yield_time_ms=...) 启动异步会话
- 会话结束后无法再写入
- wait_for 可用于轮询直到输出中出现特定内容
- close_stdin 后仍可读取输出，但不能再写入
