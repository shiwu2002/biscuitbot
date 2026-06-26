# find_files

根据条件查找文件。

## 何时使用

需要按名称、类型或路径查找文件时使用。支持 glob 模式匹配和多种过滤条件，适合在大型项目中快速定位文件。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| path | string | 否 | 当前目录 | 搜索的根目录 |
| query | string | 否 | - | 文件名查询关键字 |
| glob | string | 否 | - | glob 匹配模式（如 *.py） |
| type | string | 否 | - | 文件类型过滤（f/d） |
| include_dirs | boolean | 否 | false | 是否包含目录结果 |
| sort | string | 否 | name | 排序方式（name/size/time） |
| head_limit | integer | 否 | - | 返回结果数量上限 |
| offset | integer | 否 | 0 | 跳过前 N 条结果 |

## 调用示例

```
find_files(path="/home/user", glob="*.log", sort="time", head_limit=10)
```

## 注意事项

- glob 与 query 可组合使用，也可单独使用
- 大目录搜索可能耗时，建议限定 path 范围
- offset 与 head_limit 配合可实现分页
- 路径必须为绝对路径
