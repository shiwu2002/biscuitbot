# register_tool

自定义工具注册元工具。

## 何时使用

需要将自定义编写的工具集成到工具系统中时使用。通过指定工具文件和文档路径完成注册，注册后可通过 discover_tools 发现和调用。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| file_path | string | 是 | - | 工具实现 Python 文件路径（1-500 字符）。文件需包含唯一的 Tool 子类，实现 name/description/parameters/execute，设置 _capability 和 _usage_md |
| docs_md_path | string | 是 | - | 工具用法文档 markdown 文件路径（1-500 字符）。需描述参数、调用示例和注意事项 |

## 调用示例

```
register_tool(file_path="/tools/my_tool.py", docs_md_path="/tools/docs/my_tool.md")
```

## 注意事项

- 注册成功后工具立即可用
- 文件路径长度限制 1-500 字符
- 文档路径长度限制 1-500 字符
- 可随时通过 unregister_tool 卸载
