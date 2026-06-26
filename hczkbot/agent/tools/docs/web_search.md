# web_search

搜索网络获取实时信息。

## 何时使用

需要查询最新资讯、技术文档或超出知识截止日期的信息时使用。返回搜索结果摘要和链接，适合获取实时数据。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 搜索关键词 |
| count | integer | 否 | 5 | 返回结果数量 |
| timeRange | string | 否 | - | 时间范围过滤（day/week/month/year） |

## 调用示例

```
web_search(query="Python 3.12 新特性", count=10, timeRange="month")
```

## 注意事项

- query 应简洁明确，避免过于宽泛
- count 过大会增加响应时间，建议 5-10
- timeRange 用于过滤时效性内容
- 返回结果包含摘要和链接，详细内容请使用 web_fetch
