# 10. 接入示例

## 10.1 Node.js (Express) 示例

```javascript
const express = require('express');
const jwt = require('jsonwebtoken');
const app = express();
app.use(express.json());

const SHARED_SECRET = process.env.JWT_AGENT_SHARED_SECRET || 'hczk-ai-platform-secret-key-2024-very-long-and-secure';

function verifyPlatformJWT(req, res, next) {
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: '缺少认证信息' });
  }
  try {
    const token = authHeader.substring(7);
    const payload = jwt.verify(token, SHARED_SECRET, { issuer: 'hczk-platform' });
    req.user = {
      id: payload.userId,
      apiKey: payload.apiKey || null
    };
    next();
  } catch (err) {
    return res.status(401).json({ error: 'JWT 验证失败: ' + err.message });
  }
}

// 健康检测（无需认证）
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', version: '1.0.0', uptime: process.uptime(), timestamp: new Date().toISOString() });
});

// 对话接口（需 JWT 认证，接收工具信息）
app.post('/api/chat', verifyPlatformJWT, async (req, res) => {
  const { message, session_id, user_id, available_skills,
          tools_discovery_endpoint, tools_execution_endpoint, tools_auth_token } = req.body;

  // 如果智能体需要调用平台工具：
  // 1. 查询工具组详情
  //    GET tools_discovery_endpoint + '/knowledge'
  //    Authorization: Bearer {tools_auth_token}
  // 2. 执行工具
  //    POST tools_execution_endpoint
  //    Authorization: Bearer {tools_auth_token}
  //    body: { tool_name: 'knowledge_search', arguments: { query: '...', user_id: user_id } }
  //    注意：user_id 为必填参数，必须从对话请求的 user_id 字段获取并携带
  // 3. 使用结果中的溯源信息增强回复

  const reply = await processMessage(message, session_id, user_id);
  res.json({ reply, session_id, metadata: { model: 'deepseek-chat' } });
});

// 流式对话接口
app.post('/api/chat/stream', verifyPlatformJWT, async (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');

  res.write(`data: ${JSON.stringify({ type: 'thinking', content: '意图识别中...' })}\n\n`);
  res.write(`data: ${JSON.stringify({ type: 'content', content: '您好！' })}\n\n`);
  res.write(`data: ${JSON.stringify({ type: 'done' })}\n\n`);
  res.end();
});

// 智能体元信息（无需认证）
app.get('/api/agent/info', (req, res) => {
  res.json({
    name: '客服智能体', version: '1.0.0',
    capabilities: { knowledge_retrieval: true, tool_calling: true, streaming: true },
    model: 'deepseek-chat',
    tools: [{ name: 'query_order', description: '查询订单状态' }]
  });
});

app.listen(3000, () => console.log('Agent running on port 3000'));
```

## 10.2 Python (FastAPI) 示例

```python
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import jwt, httpx, os, time, json, asyncio

app = FastAPI()
SHARED_SECRET = os.environ.get("JWT_AGENT_SHARED_SECRET", "hczk-ai-platform-secret-key-2024-very-long-and-secure")

class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str
    collection_name: str | None = None
    context: list | None = None
    available_skills: list | None = None
    tools_discovery_endpoint: str | None = None
    tools_execution_endpoint: str | None = None
    tools_auth_token: str | None = None

def verify_platform_jwt(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少认证信息")
    token = auth_header[7:]
    try:
        payload = jwt.decode(token, SHARED_SECRET, algorithms=["HS256"], issuer="hczk-platform")
        return {"user_id": payload["userId"], "api_key": payload.get("apiKey"), "token": token}
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"JWT 验证失败: {e}")

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "uptime": int(time.time() - start_time)}

@app.post("/api/chat")
async def chat(req: ChatRequest, user=Depends(verify_platform_jwt)):
    # 智能体可按需调用平台工具：
    # if req.tools_discovery_endpoint and req.tools_auth_token:
    #     url = req.tools_discovery_endpoint + "/knowledge"
    #     headers = {"Authorization": f"Bearer {req.tools_auth_token}"}
    #     async with httpx.AsyncClient() as client:
    #         tools = await client.get(url, headers=headers)
    #     # ... 使用 tools 进行 function calling ...
    #     # 执行工具时：
    #     result = await client.post(
    #         req.tools_execution_endpoint,
    #         headers={"Authorization": f"Bearer {req.tools_auth_token}"},
    #         json={"tool_name": "knowledge_search", "arguments": {"query": "...", "user_id": req.user_id}}
    #     )
    #     # user_id 为必填参数，从对话请求的 user_id 字段获取
    reply = await process_message(req.message, req.session_id, req.user_id)
    return {"reply": reply, "session_id": req.session_id}

@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user=Depends(verify_platform_jwt)):
    async def generate():
        yield f"data: {json.dumps({'type': 'thinking', 'content': '意图识别中...'})}\n\n"
        yield f"data: {json.dumps({'type': 'content', 'content': '您好！'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/api/agent/info")
async def agent_info():
    return {"name": "客服智能体", "version": "1.0.0",
            "capabilities": {"knowledge_retrieval": True, "tool_calling": True, "streaming": True},
            "model": "deepseek-chat", "tools": [{"name": "query_order"}]}
```
