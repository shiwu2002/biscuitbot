# 7. 接口详细规范

## 7.1 健康检测接口（必填）

**请求**

```
GET {health_endpoint}
```

> 健康检测接口不携带 JWT 认证头。如果注册时配置了 `auth_header`，平台仍会携带该认证头用于兼容旧版智能体。

**响应**

```json
{
  "status": "ok",
  "version": "1.2.0",
  "uptime": 86400,
  "timestamp": "2026-06-16T10:30:00Z",
  "components": {
    "database": {"status": "connected", "latency_ms": 5},
    "knowledge": {"status": "connected", "collection": "products", "document_count": 1234},
    "llm": {"status": "available", "model": "deepseek-chat"}
  },
  "runtime": {"node_version": "v20.11.0", "memory_usage_mb": 256, "active_sessions": 12}
}
```

**字段说明**：见上表，响应格式灵活，智能体可自定义 `components` 和 `runtime` 结构。

**状态判定规则**

| status 值 | 平台显示 | 含义 |
|-----------|---------|------|
| `ok` | 在线（绿色） | 服务完全正常 |
| `degraded` | 降级（黄色） | 部分功能受限但仍可工作 |
| 其他值 / 请求超时 | 离线（红色） | 服务不可用 |

**超时要求**：接口应在 **3 秒内** 返回响应，否则平台判定为离线。

---

## 7.2 对话接口（必填）

**请求**

```
POST {chat_endpoint}
Content-Type: application/json
Authorization: Bearer {jwt_token}
```

**请求体**

```json
{
  "message": "你好，请问有什么产品推荐？",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "1234567890123456789",
  "collection_name": "products",
  "context": [
    {"role": "user", "content": "之前的对话内容"},
    {"role": "assistant", "content": "之前的回复内容"}
  ],
  "available_skills": [
    {"name": "knowledge", "display_name": "知识库", "description": "知识库操作工具组", "tool_count": 5}
  ],
  "tools_discovery_endpoint": "http://平台地址:8080/api/tools/group",
  "tools_execution_endpoint": "http://平台地址:8080/api/tools/execute",
  "tools_auth_token": "sk-hczk-xxx"
}
```

**请求字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string | 是 | 用户消息 |
| `session_id` | string | 是 | 会话 ID |
| `user_id` | string | 是 | 用户ID（雪花ID），智能体据此查找用户的知识库集合，调用工具时必须携带 |
| `collection_name` | string | 否 | 知识库集合名，默认 `default` |
| `context` | array | 否 | 对话上下文 |
| `available_skills` | array | 否 | 可用工具组目录（已按用户权限过滤），每个条目含 name、display_name、description、tool_count |
| `tools_discovery_endpoint` | string | 否 | 工具详情查询端点，追加具体工具组名（如 `/knowledge`）后 GET 请求获取该组所有工具 |
| `tools_execution_endpoint` | string | 否 | 工具执行端点，POST 请求执行工具 |
| `tools_auth_token` | string | 否 | 工具调用认证令牌，作为 `Authorization: Bearer {token}` 头使用 |

**响应**

```json
{
  "reply": "根据您的需求，我推荐以下产品...",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "metadata": {
    "input_tokens": 150,
    "output_tokens": 320,
    "model": "deepseek-chat",
    "tool_calls": 2,
    "retrieval_count": 3,
    "thinking_content": "用户询问产品推荐，需要检索知识库...",
    "retrieval_used": true,
    "fallback_used": false
  }
}
```

**响应字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reply` | string | **必填** | 智能体回复内容 |
| `session_id` | string | 可选 | 会话 ID（建议返回） |
| `metadata` | object | 可选 | 元数据，结构自定义 |
| `metadata.input_tokens` | number | 可选 | 输入 Token 数（用于计费统计） |
| `metadata.output_tokens` | number | 可选 | 输出 Token 数（用于计费统计） |
| `metadata.thinking_content` | string | 可选 | AI 思考过程（启用思考模式时返回） |
| `metadata.retrieval_used` | boolean | 可选 | 是否使用了知识库检索 |
| `metadata.fallback_used` | boolean | 可选 | 是否使用了降级回复 |

**错误响应**

```json
{
  "error": "LLM 服务暂时不可用",
  "code": "LLM_UNAVAILABLE",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## 7.3 流式对话接口（可选，P0 优先级）

**请求**

```
POST {stream_endpoint}
Content-Type: application/json
Authorization: Bearer {jwt_token}
```

**请求体**：与 `POST /api/chat` 完全一致（含 `available_skills`、`tools_discovery_endpoint`、`tools_execution_endpoint`、`tools_auth_token`）。

**响应**：`Content-Type: text/event-stream`，每行格式 `data: {JSON}\n\n`

> **重要**：每个 SSE 事件必须以 `\n\n`（两个换行符）结尾，否则多事件会被拼接在一起。

**事件类型**

| type | 说明 |
|------|------|
| `thinking` | 思考/意图识别过程 |
| `retrieval` | 知识库检索 |
| `tool` | 工具调用 |
| `content` | 回复内容片段 |
| `done` | 结束 |

**降级策略**：如果智能体未配置 `stream_endpoint`，平台自动降级为同步调用。

---

## 7.4 智能体元信息接口（可选，P0 优先级）

**请求**

```
GET {info_endpoint}
```

> 元信息接口不携带 JWT 认证头，平台直接调用。

**响应**

```json
{
  "name": "客服智能体",
  "version": "1.0.0",
  "description": "支持订单查询、商品推荐、工单创建的客服智能体",
  "capabilities": {
    "knowledge_retrieval": true,
    "tool_calling": true,
    "multi_turn": true,
    "streaming": true,
    "thinking": true
  },
  "model": "deepseek-v4-pro",
  "tools": [
    { "name": "query_order", "description": "查询订单状态" },
    { "name": "search_product", "description": "搜索商品信息" },
    { "name": "create_ticket", "description": "创建工单" }
  ]
}
```

**字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 智能体显示名称 |
| `version` | string | 否 | 版本号 |
| `description` | string | 否 | 功能描述 |
| `capabilities` | object | 否 | 能力标签，平台据此展示功能标签 |
| `capabilities.knowledge_retrieval` | boolean | 否 | 是否支持知识库检索 |
| `capabilities.tool_calling` | boolean | 否 | 是否支持工具调用 |
| `capabilities.multi_turn` | boolean | 否 | 是否支持多轮对话 |
| `capabilities.streaming` | boolean | 否 | 是否支持流式输出 |
| `capabilities.thinking` | boolean | 否 | 是否支持思考模式 |
| `model` | string | 否 | 使用的 LLM 模型 |
| `tools` | array | 否 | 可用工具列表 |
| `tools[].name` | string | 是 | 工具名称 |
| `tools[].description` | string | 否 | 工具描述 |

**平台使用场景**：
1. 智能体卡片展示能力标签：`知识库` `工具调用` `流式输出`
2. 展示可用工具列表
3. 展示模型信息

**降级策略**：如果未配置 `info_endpoint`，平台从数据库中读取注册时填写的基本信息。

---

## 7.5 会话历史接口（可选，P1 优先级）

**请求**

```
GET {history_endpoint}/{session_id}
Authorization: Bearer {jwt_token}
```

**响应**

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "messages": [
    { "role": "user", "content": "我的订单到哪了" },
    { "role": "assistant", "content": "您的订单已发货，预计明天到达。" },
    { "role": "user", "content": "可以退货吗" },
    { "role": "assistant", "content": "可以的，请在7天内申请退货。" }
  ],
  "message_count": 4
}
```

**字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 会话 ID |
| `messages` | array | 是 | 消息列表，按时间顺序 |
| `messages[].role` | string | 是 | 角色：`user` / `assistant` / `system` |
| `messages[].content` | string | 是 | 消息内容 |
| `message_count` | number | 否 | 消息总数 |

---

## 7.6 清除会话接口（可选，P2 优先级）

**请求**

```
DELETE {history_endpoint}/{session_id}
Authorization: Bearer {jwt_token}
```

**响应**

```json
{
  "status": "ok",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

> 注意：清除会话复用 `history_endpoint` 地址，通过 HTTP 方法区分 GET（获取历史）和 DELETE（清除会话）。

---

## 7.7 文档上传接口（可选，P1 优先级）

**请求**

```
POST {document_endpoint}
Content-Type: multipart/form-data
Authorization: Bearer {jwt_token}
```

**表单字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | File | **必填** | 上传的文档文件 |
| `collection_name` | string | **必填** | 知识库集合名称 |
| `user_id` | string | 可选 | 用户雪花ID，用于定位该用户的 Milvus 物理集合 |
| `document_type` | string | 可选 | 文档类型：`pdf`/`txt`/`md`/`docx` 等 |

**响应**

```json
{
  "status": "ok",
  "document_id": "doc_abc123",
  "collection_name": "products",
  "chunk_count": 42,
  "message": "文档已成功入库"
}
```
