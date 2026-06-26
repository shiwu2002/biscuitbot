# cold_storage

搜索冷门仓库中已被轮转出的工具和技能 (Search cold storage for tools rotated out of the active index)。

## 何时使用

- 当 INDEX.md 中的按需工具和技能不满足当前需求时
- 当你记得某个工具存在但 INDEX.md 中找不到时
- 定期检查冷门仓库是否有可复用的工具

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| query | string | 是 | - | 关键词或工具名，用于搜索冷门仓库 |
| limit | integer | 否 | 5 | 最大返回数量（上限 10） |

## 调用示例

```
cold_storage(query="image processing")
cold_storage(query="list all")
cold_storage(query="weather", limit=3)
```

## 注意事项

- 冷门工具是被定时任务从活跃索引中移除的（超过 14 天未调用）
- 找到冷门工具后，用 `discover_tools("工具名")` 加载其 schema
- 冷门工具被调用后会自动恢复到活跃索引中
- `query="list all"` 可查看冷门仓库中的全部工具
