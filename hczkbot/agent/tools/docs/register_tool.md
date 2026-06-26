# register_tool

注册自定义工具的元工具。

## 何时使用

需要将自定义编写的工具集成到工具系统中时使用。通过指定工具文件和文档路径完成注册，适合扩展系统能力。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| file_path | string | 是 | - | 工具实现文件绝对路径 |
| docs_md_path | string | 是 | - | 工具说明文档路径 |

## 调用示例

```
register_tool(file_path="/tools/my_tool.py", docs_md_path="/tools/docs/my_tool.md")
```

## 注意事项

- file_path 指向的工具实现必须符合规范
- docs_md_path 指向的文档需包含工具 schema 说明
- 注册后工具立即可用
- 重复注册同名工具会覆盖原定义
