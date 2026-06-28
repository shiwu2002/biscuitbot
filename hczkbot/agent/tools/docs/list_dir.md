 # list_dir

列出目录内容。

## 何时使用

需要查看目录结构、浏览文件系统或在不确定文件位置时定位资源。支持递归模式和最大条目限制。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 是 | - | 要列出的目录路径 |
| recursive | boolean | 否 | false | 是否递归列出子目录 |
| max_entries | integer | 否 | 200 | 最大返回条目数（0 表示无限制） |

## 调用示例

```
list_dir(path="/src")
list_dir(path="/src", recursive=true)
list_dir(path="/src", recursive=true, max_entries=50)
```

## 注意事项

- 返回结果包含文件和目录
- 自动跳过常见的临时目录（如 __pycache__, node_modules）
- 结果过多时会被截断，建议用 max_entries 控制
