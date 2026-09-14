 # discover_employees

按技能/分类/姓名检索可调用的数字员工。调用 `invoke_employee` 之前先检索，避免凭名单盲目点名。

## 何时使用

- 团队规模较大、需要挑选最合适员工时；
- 不知道哪位数字员工能胜任当前任务时；
- 想了解人才市场里有哪些可用的新员工时。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 检索关键词，可匹配员工英文代号、中文姓名、职位或技能（长度 1-200 字符） |
| limit | integer | 否 | 10 | 最大返回员工数（1-20） |

## 调用示例

```
discover_employees(query="剪辑")
discover_employees(query="seedance")
discover_employees(query="文案", limit=5)
```

## 返回说明

返回 JSON，每条员工含 `id` / `name` / `title` / `skills` / `installed` / `source`：

- `installed: true` 表示已安装，可直接用 `invoke_employee` 调用；
- `installed: false` 表示人才市场中的未安装员工，**仅供提示**，需用户在 WebUI
  手动安装后才能调用（本工具不会自动安装）。

## 注意事项

- 未配置人才市场注册表、或目录拉取失败时，只会返回已安装员工并在 note 中说明；
- 检索范围同时覆盖已安装员工与人才市场目录，按 id 去重、已安装优先；
- 团队规模较大时，务必先 `discover_employees` 检索最合适的员工，再 `invoke_employee` 点名。
