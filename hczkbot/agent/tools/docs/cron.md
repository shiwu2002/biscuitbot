# cron

管理定时任务和提醒。

## 何时使用

需要周期性执行任务、设置定时提醒或调度自动化流程时使用。支持固定间隔、cron 表达式和一次性执行三种调度方式。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 操作类型（add/list/remove） |
| name | string | 否 | 取 message 前 30 字符 | 任务简短标签 |
| message | string | add 时必填 | - | 触发时执行的内容（如 "Send a reminder to WeChat: xxx"） |
| every_seconds | integer | 否 | - | 执行间隔（秒），用于循环任务 |
| cron_expr | string | 否 | - | cron 表达式（如 '0 9 * * *'），用于定时任务 |
| at | string | 否 | - | ISO 日期时间字符串，用于一次性执行（如 '2026-02-12T10:30:00'），无时区值时使用工具默认时区 |
| tz | string | 否 | 工具默认时区 | IANA 时区（如 'Asia/Shanghai', 'America/Vancouver'），用于 cron_expr 和 at |
| job_id | string | remove 时必填 | - | 要删除的任务 ID（通过 list 获取） |

## 调用示例

```
# 添加 cron 定时任务
cron(action="add", name="daily_report", cron_expr="0 9 * * *", message="生成日报", tz="Asia/Shanghai")

# 添加间隔任务
cron(action="add", name="health_check", every_seconds=3600, message="健康检查并报告")

# 一次性任务
cron(action="add", at="2026-03-15T15:00:00", message="提醒用户会议", tz="Asia/Shanghai")

# 列出所有任务
cron(action="list")

# 删除任务
cron(action="remove", job_id="abc-123")
```

## 注意事项

- action="add" 需要 message 和一种调度方式（every_seconds/cron_expr/at 三选一）
- action="remove" 需要 job_id（通过 action="list" 获取）
- action="list" 无需额外参数
- 跨时区场景需明确指定 tz
