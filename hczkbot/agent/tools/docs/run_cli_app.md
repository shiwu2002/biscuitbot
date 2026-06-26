# run_cli_app

运行已注册的 CLI 应用。

## 何时使用

需要调用预定义的命令行工具或应用时使用。区别于 exec 的通用命令执行，专用于已注册的 CLI 应用，参数更规范。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| name | string | 是 | - | CLI 应用名称 |
| args | string | 否 | - | 传递给应用的参数 |
| working_dir | string | 否 | 当前目录 | 工作目录 |

## 调用示例

```
run_cli_app(name="git", args="status", working_dir="/home/user/project")
```

## 注意事项

- name 必须为已注册的 CLI 应用
- args 按应用要求格式化，空格分隔
- 应用未注册会返回错误
- 交互式应用可能无法正常运行
