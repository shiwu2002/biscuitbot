# 4. MCP 工具调用（Skills）

## 4.1 概述

平台为智能体提供了 **MCP（Model Context Protocol）风格的工具调用机制**。工具按**工具组（Skill）** 组织，每个工具组包含若干可被 function calling 调用的工具（如知识库检索、文本摄入等）。

**特点**：
- **渐进加载**：对话时只传递工具组目录，智能体按需查询工具详情
- **统一执行**：内置工具统一走 `POST /api/tools/execute`，API 类工具走自定义端点
- **JWT 鉴权**：所有工具 API 复用对话时的 JWT Token 或 `tools_auth_token`
- **可见性控制**：工具组支持 public/private 可见性，private 工具组仅绑定用户可访问
- **用户身份传递**：需要用户隔离的工具（如 knowledge_search），智能体必须在调用参数中携带 `user_id`，平台不自动注入，确保工具使用人与集合归属一致

## 4.2 工具组与工具的关系

```
Skill（工具组）                  ToolDefinition（工具）
┌────────────────────┐          ┌──────────────────────────────┐
│ name: "knowledge"   │          │ name: "knowledge_search"      │
│ displayName: "知识库"│───1:N──▶│ description: "检索知识库..."    │
│ visibility: public  │          │ inputSchema: {type, params}   │
│ tool_count: 5       │          │ endpoint: /api/tools/execute   │
└────────────────────┘          └──────────────────────────────┘
```

## 4.3 Skills 可见性与用户绑定

### 4.3.1 可见性类型

| 可见性 | 说明 | 谁可查看 | 谁可调用 |
|--------|------|---------|---------|
| `public` | 公开工具组 | 所有智能体 | 所有智能体 |
| `private` | 私有工具组 | 仅绑定用户对应的智能体 | 仅绑定用户对应的智能体 |

### 4.3.2 用户绑定

管理员可在管理后台将私有工具组绑定到指定用户。绑定关系存储在 `user_skill_binding` 表中：

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | VARCHAR(64) | 用户雪花ID |
| `skill_id` | BIGINT | 工具组ID |
| `enabled` | TINYINT(1) | 是否启用（1=启用/0=禁用） |

### 4.3.3 访问控制规则

智能体调用平台工具时，平台按以下规则校验访问权限：

1. 从请求中解析用户身份（优先级：请求参数 `user_id` > JWT/API Key 认证（从 `userId` claim 提取） > 工具参数 `arguments.user_id`）
2. 查询目标工具所属的工具组可见性
3. 如果工具组为 `public` → 允许访问
4. 如果工具组为 `private` → 检查 `user_skill_binding` 中是否存在且 `enabled=1` 的绑定记录
5. 无权访问 → 返回 `{success: false, error: "无权访问该工具组"}`

## 4.4 完整调用示例

以下是智能体一次完整对话中使用工具的典型流程：

```
1. 平台 → 智能体：POST /api/chat（或 /api/chat/stream）
   {
     "message": "帮我查一下产品价格",
     "session_id": "abc123",
     "user_id": "1234567890123456789",
     "available_skills": [
       {"name": "knowledge", "display_name": "知识库", "description": "...", "tool_count": 5}
     ],
     "tools_discovery_endpoint": "http://平台地址:8080/api/tools/group",
     "tools_execution_endpoint": "http://平台地址:8080/api/tools/execute",
     "tools_auth_token": "sk-hczk-xxx"
   }

2. 智能体分析用户意图 → 需要检索知识库 → 查询工具详情
   GET http://平台地址:8080/api/tools/group/knowledge
   Authorization: Bearer {tools_auth_token}

   ← 返回 knowledge 组下 5 个工具的完整定义（name, description, parameters, endpoint）

3. 智能体将工具注册为 function，LLM 决定调用 knowledge_search
   → 智能体发送 HTTP 请求执行工具：

   POST http://平台地址:8080/api/tools/execute
   Authorization: Bearer {tools_auth_token}
   {
     "tool_name": "knowledge_search",
     "arguments": {
       "query": "产品价格",
       "user_id": "1234567890123456789"
     }
   }

   注意：user_id 为必填参数，智能体必须从对话请求的 user_id 字段获取并携带。
   collection_name 可选，默认为 "default"。

   ← 平台返回：
   {
     "code": 200,
     "data": {
       "success": true,
       "chunks": [
         {
           "content": "产品A价格为299元...",
           "score": 0.95,
           "source": "product_manual.pdf",
           "metadata": {
             "chunkType": "prose",
             "pageNumber": 3,
             "chapter": "第二章 产品定价",
             "contextPages": "2-4",
             "sourceFilename": "product_manual.pdf"
           }
         }
       ],
       "total": 1,
       "strategy": "hybrid"
     }
   }

4. 智能体将检索结果作为上下文，LLM 生成最终回复
   → SSE: data: {"type":"content","content":"根据知识库检索..."}
```

## 4.5 智能体端实现要点

**Step 1 — 接收工具组目录和执行信息**

对话请求中的 `available_skills`、`tools_discovery_endpoint`、`tools_execution_endpoint` 和 `tools_auth_token` 是可选的。如果不存在（或为空数组），说明该用户没有可用工具，跳过工具调用逻辑即可。

| 字段 | 说明 | 用途 |
|------|------|------|
| `available_skills` | 可用工具组目录（已按用户权限过滤） | 展示给 LLM 决定是否调用 |
| `tools_discovery_endpoint` | 工具详情查询端点 | GET 请求获取工具定义 |
| `tools_execution_endpoint` | 工具执行端点 | POST 请求执行工具 |
| `tools_auth_token` | 工具调用认证令牌 | 作为 `Authorization: Bearer {token}` 头 |

**Step 2 — 按需查询工具**

不要一收到请求就查询所有工具组。根据用户意图判断是否需要工具，例如用户问"查一下"、"帮我搜索"时才去查询 `knowledge` 组。

```javascript
// 仅当需要时查询
if (userIntentNeedsKnowledge && req.body.tools_discovery_endpoint) {
  const url = req.body.tools_discovery_endpoint + '/knowledge';
  const token = req.body.tools_auth_token || req.headers.authorization.replace('Bearer ', '');
  const tools = await fetch(url, {
    headers: { 'Authorization': `Bearer ${token}` }
  }).then(r => r.json());

  // tools = [{type:"function", function:{name,description,parameters}, endpoint}, ...]
  // 注册进 LLM 的 tool registry
}
```

**Step 3 — 执行工具**

每个工具返回的 `endpoint` 字段即执行地址。内置工具指向 `http://平台/api/tools/execute`，API 类工具指向各自的外部 URL。

```javascript
const tool = tools.find(t => t.function.name === 'knowledge_search');
const token = req.body.tools_auth_token || req.headers.authorization.replace('Bearer ', '');
const result = await fetch(tool.endpoint, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`
  },
  body: JSON.stringify({
    tool_name: 'knowledge_search',
    arguments: {
      query: '产品价格',
      user_id: req.body.user_id   // 必填！从对话请求的 user_id 获取
      // collection_name 可选，默认 "default"
    }
  })
}).then(r => r.json());
// result.data 中包含工具执行结果
```

**Step 4 — 使用结果**

将 `result.data` 中的内容注入到 LLM 的上下文中，由 LLM 生成最终回复。如果工具返回 `{success: false}`，告知用户操作失败。

**Step 5 — 展示溯源信息**

检索结果中每个 chunk 的 `metadata` 包含溯源信息，智能体可在回复中引用出处：

| 溯源字段 | 类型 | 说明 |
|---------|------|------|
| `pageNumber` | Integer | 原始文档页码 |
| `chapter` | String | 所属章节 |
| `contextPages` | String | 关联上下文页码范围（如 "2-4"） |
| `sourceFilename` | String | 源文件名 |
| `chunkType` | String | 块类型：prose/qa/image/table |
| `tableHtml` | String | 表格的 HTML 结构（仅表格块） |

## 4.6 工具发现 API

### 4.6.1 获取工具组目录

```
GET /api/tools/groups
Authorization: Bearer {tools_auth_token}
```

智能体收到 `tools_discovery_endpoint` 后，后追加具体工具组名即可（如 + "/knowledge"）。

**响应**

```json
[
  {
    "name": "knowledge",
    "display_name": "知识库",
    "description": "知识库相关操作工具组，包含检索、摄入、文件上传等工具",
    "category": "knowledge",
    "visibility": "public",
    "tool_count": 5
  }
]
```

### 4.6.2 获取用户可访问的工具组目录

```
GET /api/tools/groups/user?user_id={user_id}
Authorization: Bearer {tools_auth_token}
```

返回当前用户可访问的工具组（public + 已绑定的 private）。

**响应**：格式同上，但仅包含该用户有权访问的工具组。

### 4.6.3 获取工具组内工具详情

```
GET /api/tools/group/{skill_name}
Authorization: Bearer {tools_auth_token}
```

**响应**（LLM function_call 格式）

```json
[
  {
    "type": "function",
    "function": {
      "name": "knowledge_search",
      "description": "从知识库中检索与查询相关的文档内容。使用混合检索策略（向量检索+关键词检索），返回最相关的文档分块及其相关性得分和溯源信息。",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "检索查询文本"},
          "collection_name": {"type": "string", "default": "default", "description": "知识库集合名称，默认为default"},
          "user_id": {"type": "string", "description": "用户ID（雪花ID），智能体必须携带此参数用于定位用户的知识库集合"},
          "top_k": {"type": "integer", "default": 5, "description": "返回结果数量"}
        },
        "required": ["query", "user_id"]
      }
    },
    "endpoint": "http://平台地址:8080/api/tools/execute"
  }
]
```

返回字段说明

| 字段 | 说明 |
|------|------|
| `type` | 固定为 `"function"` |
| `function.name` | 工具唯一名称，执行时使用 |
| `function.description` | 工具功能描述，LLM 据此判断是否调用 |
| `function.parameters` | JSON Schema 格式的输入参数定义 |
| `endpoint` | 工具执行端点（内置工具为 `/api/tools/execute`，API 工具为自定义 URL） |

## 4.7 工具执行 API

```
POST /api/tools/execute
Content-Type: application/json
Authorization: Bearer {tools_auth_token}
```

**请求体**

```json
{
  "tool_name": "knowledge_search",
  "arguments": {
    "query": "产品价格",
    "user_id": "1234567890123456789"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tool_name` | string | 是 | 工具名称（与 function.name 一致） |
| `arguments` | object | 是 | 工具参数，与 inputSchema 对应 |
| `arguments.user_id` | string | **必填** | 用户雪花ID，**智能体必须携带**，用于定位用户的知识库集合 |

> **重要**：平台不会自动注入 `user_id`。智能体必须在调用工具时从对话请求的 `user_id` 字段获取并携带此参数。如果缺失，平台将返回错误：`"缺少必填参数: user_id，智能体必须在调用时携带"`。

**成功响应（知识库检索）**

```json
{
  "code": 200,
  "data": {
    "success": true,
    "chunks": [
      {
        "content": "产品A价格为299元，产品B价格为599元...",
        "score": 0.95,
        "source": "product_manual.pdf",
        "metadata": {
          "chunkType": "prose",
          "pageNumber": 3,
          "chapter": "第二章 产品定价",
          "contextPages": "2-4",
          "sourceFilename": "product_manual.pdf",
          "tableHtml": null
        }
      },
      {
        "content": "| 产品 | 价格 | 库存 |\n|---|---|---|\n| A | 299 | 100 |",
        "score": 0.87,
        "source": "product_manual.pdf",
        "metadata": {
          "chunkType": "table",
          "pageNumber": 4,
          "chapter": "第二章 产品定价",
          "contextPages": "4-5",
          "sourceFilename": "product_manual.pdf",
          "tableHtml": "<table><tr><th>产品</th><th>价格</th><th>库存</th></tr>...</table>"
        }
      }
    ],
    "total": 2,
    "strategy": "hybrid"
  }
}
```

**错误响应**

```json
{
  "code": 200,
  "data": {
    "success": false,
    "error": "无权访问该工具组"
  }
}
```

**缺少必填参数响应**

```json
{
  "code": 500,
  "message": "缺少必填参数: user_id，智能体必须在调用时携带"
}
```

**权限不足响应**

```json
{
  "code": 200,
  "data": {
    "success": false,
    "error": "无权访问工具组 [private_skill]，该工具组为私有且未绑定当前用户"
  }
}
```

## 4.8 内置知识库工具

应用启动时自动注册到 `knowledge` 工具组，无需手动创建。

| 工具名称 | 显示名 | 功能 | 关键参数 |
|---------|--------|------|---------|
| `knowledge_search` | 知识库检索 | 混合检索返回相关分块及溯源信息 | query(必填), user_id(必填), collection_name(可选), top_k |
| `knowledge_ingest` | 知识库文本摄入 | 文本分块+向量化入 Milvus | text, user_id(必填), collection_name |
| `knowledge_ingest_file` | 知识库文件上传 | 上传文档文件入知识库（支持OCR） | file_name, collection_name |
| `knowledge_list_collections` | 列出知识库集合 | 列出所有集合及基本信息 | user_id(必填) |
| `knowledge_get_chunks` | 查看知识分块 | 分页浏览集合内的分块 | collection_name, user_id(必填) |

## 4.9 CLI-Anything 工具市场 Skill

应用启动时自动注册到 `cli-market` 工具组（name=cli-market），无需手动创建。该 Skill 为智能体提供 CLI 命令行工具的发现和安装能力。

### 4.9.1 工具清单

| 工具名称 | 显示名 | 功能 | 关键参数 |
|---------|--------|------|---------|
| `cli_tools_list` | 列出可用CLI工具 | 获取管理员已启用且同步成功的 CLI 工具列表 | 无必填参数 |
| `cli_tools_install` | 安装CLI工具 | 获取指定 CLI 工具的完整安装元数据（install_cmd、entry_point、commands 等） | cli_name(必填) |

### 4.9.2 cli_tools_list 调用示例

```
POST /api/tools/execute
Authorization: Bearer {token}
{
  "tool_name": "cli_tools_list",
  "arguments": {}
}
```

响应：
```json
{
  "success": true,
  "total": 3,
  "tools": [
    {
      "name": "jumpserver",
      "display_name": "JumpServer",
      "description": "Bastion host management...",
      "category": "devops",
      "version": "0.1.0",
      "commands_count": 15,
      "installed": false
    }
  ]
}
```

### 4.9.3 cli_tools_install 调用示例

```
POST /api/tools/execute
Authorization: Bearer {token}
{
  "tool_name": "cli_tools_install",
  "arguments": { "cli_name": "jumpserver" }
}
```

响应：
```json
{
  "success": true,
  "name": "jumpserver",
  "display_name": "JumpServer",
  "entry_point": "cli-anything-jumpserver",
  "install_cmd": "pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=jumpserver/agent-harness",
  "install_guide": {
    "primary": "pip install git+...",
    "mirrors": ["pip install git+https://ghp.ci/..."]
  },
  "commands": [
    {
      "group": "auth",
      "name": "login",
      "description": "Login to JumpServer",
      "full_command": "cli-anything-jumpserver auth login",
      "input_schema": "{...}"
    }
  ]
}
```

**智能体拿到元数据后**：自行 pip install → 通过 entry_point 和 commands[].full_command 在本地执行。

### 4.9.4 管理员启停

CLI 工具默认不在市场显示（is_enabled=false）。管理员在后台"CLI 工具市场"页面启用后，`cli_tools_list` 才返回该工具。禁用后只是市场不可见，不影响已安装的工具。

---

## 4.10 知识库集合命名规则

平台使用 Milvus 向量数据库存储知识库，物理集合名按以下规则拼接：

```
kb_{user_id}_{collection_name}
```

| 组成部分 | 说明 | 示例 |
|---------|------|------|
| `kb_` | 固定前缀 | — |
| `{user_id}` | 用户雪花ID（纯数字） | `1234567890123456789` |
| `{collection_name}` | 逻辑集合名，默认 `default` | `default`、`products` |

**示例**：用户 `1234567890123456789` 上传文档到 `products` 集合 → Milvus 物理集合名为 `kb_1234567890123456789_products`

**智能体检索知识库时**：
- `user_id` 为必填参数，**必须由智能体在调用工具时携带**（从对话请求的 `user_id` 字段获取）
- `collection_name` 可选，默认为 `"default"`
- 平台自动拼接为 `kb_{user_id}_{collection_name}` 查询 Milvus

**前端用户选择注意事项**：

平台管理后台的用户选择下拉框（知识库管理、检索测试、评分重算等场景）必须绑定 `User.userId`（雪花ID 字符串），**不能**绑定 `User.id`（users 表自增主键）。二者均为唯一标识但取值不同：

| 字段 | 类型 | 来源 | 用途 |
|------|------|------|------|
| `User.id` | Long | users 表自增主键 | 平台内部关联（如绑定表外键） |
| `User.userId` | String | 雪花算法生成 | 对外暴露的用户标识，用于 JWT、Milvus 集合命名、工具参数 |

若前端误绑 `User.id`，会导致 `agentId` 传成自增ID（如 `5`），Milvus 集合名拼成 `kb_5_default`，而实际数据存储在 `kb_{雪花ID}_default` 中，从而检索不到数据。

`/knowledge/owners/users` 接口返回的用户对象同时包含 `id` 和 `userId` 两个字段，前端取用时务必使用 `userId`。
