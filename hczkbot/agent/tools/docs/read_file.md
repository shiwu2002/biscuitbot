 # read_file

读取文件内容（文本、图片或文档）。

## 何时使用

需要查看文件内容、分析代码或读取文档时使用。支持纯文本文件，也可以读取 PDF、DOCX、XLSX、PPTX 文档和图片文件。读取图片时返回视觉内容供分析。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 是 | - | 要读取的文件路径 |
| offset | integer | 否 | 1 | 起始行号（1-indexed），用于大文件分段读取 |
| limit | integer | 否 | 2000 | 最大读取行数 |
| pages | string | 否 | - | PDF 文件页面范围（如 '1-5'），默认全部（最多 20 页） |
| force | boolean | 否 | false | 跳过去重缓存，强制重新读取（即使文件未变更） |

## 调用示例

```
# 读取整个文件
read_file(path="/tmp/README.md")

# 分段读取大文件
read_file(path="/var/log/app.log", offset=1, limit=500)
read_file(path="/var/log/app.log", offset=501, limit=500)

# 读取 PDF 前 3 页
read_file(path="/tmp/report.pdf", pages="1-3")

# 强制重新读取
read_file(path="/tmp/file.txt", force=true)
```

## 注意事项

- 输出格式为 LINE_NUM|CONTENT
- 超过 ~128K 字符的内容会被截断
- 读取前建议用 find_files 或 list_dir 确认路径
- 修改文件前需先读取当前内容，确保替换基于最新版本
- force 仅在文件内容可能被外部更改时使用
