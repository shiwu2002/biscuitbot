 # update_task

维护激活目标(active goal)的**结构化任务清单**(structured task checklist)。

## 何时使用

在 `long_task` 登记持续目标之后,用它把目标拆解为可验证的子步骤(task),并随进度持续更新状态(status)。清单写入会话元数据,每轮随 Runtime Context 重注入,让模型持续看到「做了哪些、还剩哪些」,避免长对话遗忘分析出的子任务。每完成一步或状态有变就调用一次,不要批量攒到最后。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| action | string | 是 | - | 操作:`add`(追加任务)/ `set`(更新状态或文本)/ `remove`(移除任务) |
| text | string | 否 | - | 任务文本。`add` 必填;`set`/`remove` 时作为唯一子串匹配已存在任务,≤200 字符 |
| id | string | 否 | - | 任务 id(如 `t1`),紧随每行任务在上下文中显示。`set`/`remove` 优先按 id 定位,≤32 字符 |
| status | string | 否 | - | `set` 时的新状态:`pending` / `in_progress` / `done`(对 `add`/`remove` 忽略) |

## 调用示例

```
update_task(action="add", text="实现 API")
update_task(action="add", text="跑通 CI", status="in_progress")
update_task(action="set", id="t2", status="done")
update_task(action="remove", id="t1")
update_task(action="set", status="done", text="API 文档")
```

## 注意事项

- 需先调用 `long_task` 登记激活目标,否则报错;目标完成后无法更新 → 先 `complete_goal` 或重开
- `add` 的 `text` 必填;`set` 的 `status` 必填;`remove` 优先按 `id` 定位,id 未知时可用唯一文本子串匹配
- 任务清单存于目标 blob,**压缩不会移除**;每轮末尾会提示用 `update_task` 维护状态
- 若 `id` 失效(找不到),工具会返回当前有效 id+text 列表,据此重新对账
- steps 上限 50;`complete_goal` 不会自动把未完成任务标 done,保留原始清单供审计
- 与 `long_task` / `complete_goal` 共用同一目标状态与路由,不应跨会话混用
