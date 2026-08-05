# my

检查或设置代理运行时的配置和活动目标。

## 何时使用

需要查看当前配置项、修改参数或了解运行环境时使用。支持读取和写入配置，适合运行时调整行为。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 操作类型（check/set，也接受别名 inspect/modify） |
| key | string | 否 | - | 配置项的点路径（如 'max_iterations', 'workspace', 'provider_retry_mode'），不填 key 时显示全部配置 |
| value | - | 否 | - | 新值（仅 set 时），类型需匹配目标 |

## 约束

- 可修改项：max_iterations（1–100，整数）、context_window_tokens（4096–1M）、model（字符串，非空）
- 不可修改：bus, provider, tools 等敏感内部属性（含 credential 相关字段）
- 保护：只读属性（如 subagents, exec_config, web_config, workspace_sandbox）可查看但不可修改

## 调用示例

```
# 查看所有配置
my(action="check")

# 查看指定配置
my(action="check", key="model")
my(action="check", key="max_iterations")

# 修改配置（需要确认影响）
my(action="set", key="max_iterations", value=80)
my(action="set", key="model", value="deepseek-v4-fast")
```

## 注意事项

- 修改仅在内存中生效，重启后恢复默认
- 查看 top-level key 会显示其所有属性
- 需要显式告知用户修改原因再执行 set
- action="set" 时 value 类型必须匹配（整数/字符串）
