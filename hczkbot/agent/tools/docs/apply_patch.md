# apply_patch

对多个文件批量执行编辑操作。

## 何时使用

需要同时修改多个文件或对同一文件进行多处编辑时使用。通过 edits 数组一次性提交所有修改，适合大规模重构或批量更新。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| edits | array | 是 | - | 编辑操作数组，每项含 path/old_text/new_text |
| dry_run | boolean | 否 | false | 是否仅预览不实际执行 |

## 调用示例

```
apply_patch(edits=[
  {"path": "/tmp/a.py", "old_text": "v1", "new_text": "v2"},
  {"path": "/tmp/b.py", "old_text": "v1", "new_text": "v2"}
], dry_run=false)
```

## 注意事项

- 任一编辑失败会回滚整个事务，保证原子性
- dry_run 为 true 时仅返回预览结果不修改文件
- edits 数组中每项的 old_text 必须在对应文件中唯一
- 建议先 dry_run 预览确认再正式执行
