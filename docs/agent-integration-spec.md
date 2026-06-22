# 桓宸智科 AI 平台 — 智能体接入规范

> 版本：7.0.0 | 日期：2026-06-22 | 适用范围：所有需要注册到桓宸智科 AI 平台的容器智能体

## v7.0.0 变更

- 新增 **CLI-Anything 工具市场**：智能体通过系统 Skill "CLI工具市场" 发现和安装 CLI 工具（cli_tools_list / cli_tools_install）
- CLI 工具启用/禁用分离：管理员在市场启用（is_enabled），智能体按需安装，平台不自动创建 Skill
- 同步改为管理员手动触发

## v6.0.0 变更

- `user_id` 不再由平台自动注入，**必须由智能体在调用工具时携带**
- JWT Token 新增 `userId` claim，登录时写入
- 智能体调用 JWT 中用户标识 claim 从 `user_id` 修正为 `userId`，与登录 JWT 保持一致
- 试用场景传递真实用户ID（从JWT认证上下文获取），不再硬编码 "trial"
- 工具执行前校验 required 参数，缺失时返回明确错误
- `getAgentId()` 不再回退到 "default"，缺失 user_id 时直接报错
- JwtAuthenticationFilter 支持识别 agentToken（智能体回调 JWT）
- 余额预检查改为预估费用机制（输入Token + 预估输出Token×1.5）

## 文档目录

| 章节 | 文件 | 说明 |
|------|------|------|
| 1 | [01-overview.md](spec/01-overview.md) | 概述 |
| 2 | [02-api-summary.md](spec/02-api-summary.md) | 接口规范总览 |
| 3 | [03-jwt-auth.md](spec/03-jwt-auth.md) | JWT 鉴权机制 |
| 4 | [04-mcp-tools.md](spec/04-mcp-tools.md) | MCP 工具调用（Skills） |
| 5 | [05-knowledge.md](spec/05-knowledge.md) | 知识库入库与检索 |
| 6 | [06-retrieval-monitoring.md](spec/06-retrieval-monitoring.md) | 检索监控 |
| 7 | [07-api-spec.md](spec/07-api-spec.md) | 接口详细规范 |
| 8 | [08-registration.md](spec/08-registration.md) | 注册流程 |
| 9 | [09-platform-behavior.md](spec/09-platform-behavior.md) | 平台行为说明 |
| 10 | [10-examples.md](spec/10-examples.md) | 接入示例（Node.js / Python） |
| 11 | [11-compatibility.md](spec/11-compatibility.md) | 兼容性说明 |
| 12 | [12-faq.md](spec/12-faq.md) | 常见问题 |
