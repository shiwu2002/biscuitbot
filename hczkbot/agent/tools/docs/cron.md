# cron

管理定时任务。

## 何时使用

需要周期性执行任务、设置提醒或调度自动化流程时使用。支持固定间隔和 cron 表达式两种调度方式。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 操作类型（add/list/remove） |
| name | string | 否 | - | 任务名称 |
| message | string | 否 | - | 任务触发时执行的内容 |
| every_seconds | integer | 否 | - | 执行间隔（秒） |
| cron_expr | string | 否 | - | cron 表达式 |
| at | string | 否 | - | 指定执行时间 |
| tz | string | 否 | 系统时区 | 时区 |
| job_id | string | 否 | - | 任务 ID（remove 时使用） |

## 调用示例

```
cron(action="add", name="daily_report", cron_expr="0 9 * * *", message="生成日报", tz="Asia/Shanghai")
```

## 注意事项

- action 为 add 时需提供 name 和 message
- every_seconds 与 cron_expr 二选一，不可同时使用
- remove 操作需提供 job_id
- 时区影响任务执行时间，跨时区请明确指定 tz
