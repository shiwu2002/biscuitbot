 # edit_file

在文件中替换指定文本。

## 何时使用

需要对单个文件进行精确替换时使用。通过 old_text 定位要修改的位置，用 new_text 替换。适合小范围精确编辑，不需要修改整个文件。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 是 | - | 要编辑的文件路径 |
| old_text | string | 是 | - | 需要替换的原始文本（应尽量与文件内容一致，代码也容忍缩进差异和智能引号变体） |
| new_text | string | 是 | - | 替换后的新文本 |
| replace_all | boolean | 否 | false | 是否替换所有匹配项（默认仅替换首次匹配） |
| occurrence | integer | 否 | - | 指定替换第 N 次匹配（与 replace_all 互斥） |
| line_hint | integer | 否 | - | 行号提示，帮助定位模糊匹配 |
| expected_replacements | integer | 否 | - | 预期替换次数，用于确认操作正确性 |

## 调用示例

```
# 替换首次匹配
edit_file(path="/tmp/a.py", old_text="foo", new_text="bar")

# 替换所有匹配
edit_file(path="/tmp/a.py", old_text="foo", new_text="bar", replace_all=true)

# 指定替换次数确认
edit_file(path="/tmp/a.py", old_text="foo", new_text="bar", expected_replacements=3)
```

## 注意事项

- old_text 应与文件中的原始文本一致（包括空格缩进），代码也支持行级 trim 匹配和智能引号归一化作为 fallback
- old_text 不唯一时会报错，请提供更多上下文或使用 occurrence
- 建议先从 read_file 复制 old_text，避免手动输入错误
- 升级提醒：多文件或多处编辑请优先使用 apply_patch
