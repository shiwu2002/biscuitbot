# web_fetch

抓取指定 URL 的网页内容。

## 何时使用

需要获取网页详细内容、解析文档或读取在线资源时使用。返回 markdown 格式内容，适合深度阅读网页信息。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| url | string | 是 | - | 要抓取的网页 URL |
| maxChars | integer | 否 | - | 返回内容最大字符数 |
| format | string | 否 | markdown | 输出格式（markdown/text/html） |

## 调用示例

```
web_fetch(url="https://example.com/docs", format="markdown", maxChars=5000)
```

## 注意事项

- url 必须为完整有效的 http/https 链接
- HTTP 链接会自动升级为 HTTPS
- 需要登录的页面无法抓取，会返回空内容
- 内容过大时会被截断，建议使用 maxChars 控制
