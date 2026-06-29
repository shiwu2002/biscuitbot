 # apply_patch

对多个文件批量执行编辑操作。

## 何时使用

需要同时修改多个文件或对同一文件进行多处编辑时使用。通过 edits 数组一次性提交所有修改，适合大规模重构或批量更新。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| edits | array | 是 | - | 编辑操作数组（1-20 项），每项包含 path/action/old_text/new_text |
| dry_run | boolean | 否 | false | 是否仅验证和预览，不实际写入文件 |

edits 数组中每项的结构：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 是 | - | 要编辑的文件相对路径 |
| action | string | 是 | - | 操作类型：replace（替换）或 add（添加） |
| old_text | string | 否 | - | 要替换的原文（replace 时必填） |
| new_text | string | 否 | - | 替换或添加的内容（replace/add 时必填） |

## 调用示例

```
apply_patch(edits=[
  {"path": "src/a.py", "action": "replace", "old_text": "v1", "new_text": "v2"},
  {"path": "src/b.py", "action": "add", "new_text": "print('hello')\n"}
], dry_run=true)
```

## 注意事项

- 任一编辑失败会回滚整个事务，保证原子性（由 apply_patch 工具自身实现备份与回滚）
- dry_run=true 时仅返回验证结果和预览，不修改文件
- 路径相对于工作区根目录
- edits 数组最少 1 项，最多 20 项
- 建议先 dry_run 预览确认再正式执行
