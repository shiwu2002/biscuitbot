# grep

在文件中搜索匹配正则表达式的内容。

## 何时使用

需要在工作区内搜索文本、代码片段或模式时使用。支持按文件路径、类型和 glob 过滤，适合代码分析和日志检索。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| pattern | string | 是 | - | 正则表达式或纯文本搜索模式（最小长度 1） |
| path | string | 否 | . | 搜索范围路径 |
| glob | string | 否 | - | 文件名 glob 过滤（如 *.py, **/test_*.py） |
| type | string | 否 | - | 文件类型过滤，如 'py', 'ts', 'md', 'json' |
| output_mode | string | 否 | files_with_matches | 输出模式（files_with_matches/count/content） |
| fixed_strings | boolean | 否 | false | 是否按字面字符串匹配（非正则） |
| case_insensitive | boolean | 否 | false | 是否忽略大小写 |
| context_before | integer | 否 | - | 匹配行前展示的上下文行数（0-20） |
| context_after | integer | 否 | - | 匹配行后展示的上下文行数（0-20） |
| head_limit | integer | 否 | 250 | 最多返回的结果数（0-1000，0=不限制）。content 模式限制匹配行块数，其他模式限制文件条目数 |
| offset | integer | 否 | - | 跳过前 N 个结果（配合 head_limit 分页） |
| max_matches | integer | 否 | - | head_limit 的兼容别名（content 模式，1-1000） |
| max_results | integer | 否 | - | head_limit 的兼容别名（files_with_matches/count 模式，1-1000） |

## 调用示例

```
# 搜索所有 Python 文件中的 "TODO"
grep(pattern="TODO", glob="*.py")

# 统计匹配文件数
grep(pattern="import os", output_mode="count")

# 获取匹配行内容
grep(pattern="def test_", glob="*test*", output_mode="content", context_after=2)

# 按字面串搜索（含正则特殊字符）
grep(pattern="[ERROR]", fixed_strings=true, output_mode="content")
```

## 注意事项

- 自动跳过二进制文件和超过 2MB 的大文件
- 结果过多时会自动截断，建议先用 output_mode="count" 评估规模
- content 模式下会返回行号和文件路径
- 输出上限约 128K 字符
