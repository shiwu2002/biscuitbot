 # discover_tools

按名称或功能关键词搜索可用的工具定义。

## 何时使用

当前上下文中缺少需要的工具时，使用此工具按需发现并加载工具的完整 schema。加载后即可在下一个响应中调用。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 工具名称或功能关键词（长度 1-200 字符） |
| limit | integer | 否 | 5 | 最大返回工具数（1-10） |

## 调用示例

```
discover_tools(query="screenshot")
discover_tools(query="search files", limit=3)
discover_tools(query="generate image")
```

## 注意事项

- query 会匹配工具名称和能力描述
- 返回的工具可直接在当前对话中调用
- limit 最大为 10（默认 5）
