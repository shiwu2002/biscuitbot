 # web_search

在网上搜索信息。

## 何时使用

需要查询最新信息、事实核对或获取实时数据时使用。返回标题、URL 和摘要。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 搜索查询词 |
| count | integer | 否 | 5 | 返回结果数（1-10） |
| timeRange | string | 否 | - | 时间过滤：OneDay, OneWeek, OneMonth, OneYear 或日期范围 YYYY-MM-DD..YYYY-MM-DD |
| authLevel | integer | 否 | - | 权威性过滤：0=全部, 1=仅权威来源 |
| queryRewrite | boolean | 否 | - | 是否让搜索引擎重写查询（适用于对话式或模糊查询） |

## 调用示例

```
web_search(query="Python 3.14 release date")
web_search(query="Rust async trait", count=3)
web_search(query="COVID vaccine news", timeRange="OneWeek", authLevel=1)
```

## 注意事项

- count 上限为 10（默认 5）
- 搜索结果为摘要而非完整内容，详细信息请用 web_fetch
- authLevel 和 queryRewrite 仅部分提供商支持
