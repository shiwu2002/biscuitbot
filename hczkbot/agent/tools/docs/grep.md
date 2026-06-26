# grep

在文件内容中搜索匹配文本。

## 何时使用

需要在代码或文本文件中查找特定字符串、正则模式时使用。支持上下文显示和多种输出模式，适合代码分析和日志检索。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| pattern | string | 是 | - | 正则表达式或搜索文本 |
| path | string | 否 | 当前目录 | 搜索路径 |
| glob | string | 否 | - | 文件名过滤（如 *.py） |
| type | string | 否 | - | 文件类型过滤 |
| output_mode | string | 否 | content | 输出模式（content/files/count） |
| -n | boolean | 否 | false | 是否显示行号 |
| -i | boolean | 否 | false | 是否忽略大小写 |
| -A | integer | 否 | 0 | 匹配行后显示行数 |
| -B | integer | 否 | 0 | 匹配行前显示行数 |
| -C | integer | 否 | 0 | 匹配行前后显示行数 |
| head_limit | integer | 否 | - | 结果数量上限 |

## 调用示例

```
grep(pattern="TODO", path="/src", glob="*.py", -n=true, -C=2)
```

## 注意事项

- pattern 使用正则语法，特殊字符需转义
- output_mode 为 files 时仅返回文件名，count 返回匹配数
- 大文件或大目录搜索可能耗时，建议配合 glob 缩小范围
- 默认区分大小写，需忽略请设置 -i=true
