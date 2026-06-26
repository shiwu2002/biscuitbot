# discover_tools

按需加载工具 schema 的元工具 (Request full tool definitions by name or keyword)。

## 何时使用

需要动态发现可用工具、获取工具详细定义或按需加载工具集时使用。作为元工具用于扩展可用工具范围。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 工具查询关键词或描述 |
| limit | integer | 否 | - | 返回结果数量上限 |

## 调用示例

```
discover_tools(query="文件操作", limit=10)
```

## 注意事项

- query 应描述所需工具的功能意图
- 返回的工具 schema 可用于后续调用
- limit 控制返回数量，避免结果过多
- 作为元工具，本身不执行具体操作
