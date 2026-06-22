# 9. 平台行为说明

## 9.1 健康监测

- 平台每 **30 秒** 自动调用所有智能体的健康检测接口
- 检测超时时间为 **3 秒**

## 9.2 对话路由

- 用户通过 API Key 发起对话请求 → 平台查询用户绑定（按 user_id） → 获取智能体 → 生成 JWT Token + 工具组目录 + 工具执行信息 → 调用智能体的 `chat_endpoint`
- 路由优先级：`agent_id` > `agent_endpoint`

## 9.3 工具调用

- 平台传递工具组目录、发现端点、执行端点和认证令牌
- 内置工具走 `POST /api/tools/execute`
- API 类工具走自定义 endpoint
- 工具组目录和工具定义均通过 Redis 缓存（20-30 分钟随机 TTL）
- Milvus 集合确保状态（ensuredCollections）通过 Redis 共享缓存，多实例部署时避免重复 `hasCollection` 查询
- **用户身份传递**：需要用户隔离的工具（如 knowledge_search），智能体必须在调用参数中携带 `user_id`，平台不自动注入
- **参数校验**：平台在工具执行前校验 input_schema 的 required 字段，缺失时返回明确错误
- **可见性控制**：private 工具组仅绑定用户可访问，平台在工具发现和执行时均做权限校验

## 9.4 JWT 鉴权

- 每次调用生成新 Token，有效期 1 小时
- 登录 JWT 包含 `userId` claim（雪花ID）
- 智能体调用 JWT（agentToken）也使用 `userId` claim（与登录 JWT 一致），包含 `scope` 和可选 `apiKey`
- 平台 `JwtAuthenticationFilter` 支持识别两种 Token：登录 JWT（`jwt.secret` 签名）和 agentToken（`jwt.agent.shared-secret` 签名）
- 试用场景传递真实用户ID（从JWT认证上下文获取），不再硬编码 "trial"
- 工具执行和发现 API 可使用 JWT Token 或 `tools_auth_token`

## 9.5 知识库入库

- 支持 PDF/DOCX/TXT/MD/XLSX 格式
- 扫描件自动检测并走 OCR 识别
- LLM 预处理支持页感知、跨页表格合并、篇章上下文
- 每个知识块携带溯源信息（页码、章节、源文件、关联页）

## 9.6 检索监控

- 每次检索自动记录到 `retrieval_logs` 表
- 每次入库自动记录到 `ingest_logs` 表
- 每次服务调用自动记录到 `service_health_metrics` 表
- 管理后台提供检索监控页面（统计概览、趋势图、记录列表、智能体排行、异常告警）
