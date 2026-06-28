 # long_task

开始或更新一个持续的目标追踪。

## 何时使用

需要追踪长周期任务、确保多轮对话间的目标一致性时使用。在开始大型任务时通过 goal 定义目标，用 ui_summary 提供简短的 UI 描述。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| goal | string | 是 | - | 要完成的持续性目标（需幂等且唯一，避免重复创建） |
| ui_summary | string | 否 | - | 简短的 UI 摘要，用于界面展示 |

## 调用示例

```
long_task(goal="将认证模块从 session 迁移至 JWT，所有测试通过", ui_summary="JWT 迁移")
long_task(goal="新建快件管理微服务，含 CRUD API 和单元测试，通过 CI", ui_summary="快件微服务")
```

## 注意事项

- goal 应为等幂语句，相同 goal 不会重复创建
- ui_summary 是给人看的简短标签，不是详细描述
- 目标完成后需调用 complete_goal 结束
- 一轮对话通常只有一个活动目标
