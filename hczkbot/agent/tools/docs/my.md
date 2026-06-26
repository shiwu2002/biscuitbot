# my

检查或设置当前配置。

## 何时使用

需要查看当前配置项、修改参数或了解运行环境时使用。支持读取和写入配置，适合运行时调整行为。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 操作类型（get/set/list） |
| key | string | 否 | - | 配置项键名 |
| value | string | 否 | - | 配置项值（set 时使用） |

## 调用示例

```
my(action="get", key="model")
```

```
my(action="set", key="temperature", value="0.7")
```

## 注意事项

- action 为 list 时可查看所有配置
- set 操作需同时提供 key 和 value
- 部分配置项修改后需重启生效
- 敏感配置（如密钥）不会明文返回
