# find_files

按路径片段、glob 或文件类型查找文件。

## 何时使用

需要在工作区中定位文件但不确定路径时使用。支持按名称片段、通配符或类型过滤，适合代码导航和文件发现。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 否 | . | 搜索根目录路径 |
| query | string | 否 | - | 不区分大小写的路径片段搜索，多个词用空格分隔需全部匹配 |
| glob | string | 否 | - | 文件名 glob 匹配（如 *.py, **/test_*.py） |
| type | string | 否 | - | 文件扩展名过滤（如 'py', 'ts', 'md', 'json'） |
| include_dirs | boolean | 否 | false | 是否同时返回匹配的目录 |
| sort | string | 否 | path | 排序方式（path=按路径, modified=按修改时间） |
| head_limit | integer | 否 | 200 | 返回结果上限（0 表示不限制，最大 1000） |
| offset | integer | 否 | - | 跳过前 N 个结果（配合 head_limit 分页，最大 100000） |

## 调用示例

```
find_files(path="/src")
find_files(path="/src", glob="*.py")
find_files(path="/src", query="config", type="toml")
find_files(path="/src", sort="modified", head_limit=10)
```

## 注意事项

- 返回路径均为相对于工作区的根路径
- 默认跳过隐藏文件和常见依赖/构建目录
- sort="modified" 适合查找最近修改的文件
- 结果过多时建议用 head_limit 控制
