# edit_file

对文件进行精确字符串替换。

## 何时使用

需要修改文件中的特定内容而非重写整个文件时使用。通过 old_text 精确定位并替换为 new_text，适合小范围编辑。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 是 | - | 文件绝对路径 |
| old_text | string | 是 | - | 要被替换的原文本 |
| new_text | string | 是 | - | 替换后的新文本 |
| replace_all | boolean | 否 | false | 是否替换所有匹配项 |
| occurrence | integer | 否 | - | 替换第几次出现的匹配 |
| line_hint | integer | 否 | - | 行号提示，帮助定位匹配位置 |

## 调用示例

```
edit_file(path="/tmp/config.yaml", old_text="port: 8080", new_text="port: 9090")
```

## 注意事项

- old_text 必须在文件中唯一存在，否则需指定 occurrence 或 line_hint
- 替换前会保留原文件缩进，请勿在 old_text 中包含行号前缀
- replace_all 为 true 时会替换所有匹配，请确认影响范围
- 文件不存在会返回错误
