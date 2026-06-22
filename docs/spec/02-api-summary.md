# 2. 接口规范总览

| 接口 | 方法 | 建议路径 | 必填 | 优先级 | 说明 |
|------|------|---------|------|--------|------|
| 健康检测 | GET | `/api/health` | **必填** | P0 | 平台定期调用，检测在线状态 |
| 对话 | POST | `/api/chat` | **必填** | P0 | 同步对话，返回完整回复 |
| 流式对话 | POST | `/api/chat/stream` | 可选 | P0 | SSE 格式流式输出 |
| 智能体元信息 | GET | `/api/agent/info` | 可选 | P0 | 返回能力、工具、模型信息 |
| 会话历史 | GET | `/api/chat/history/{session_id}` | 可选 | P1 | 获取指定会话历史消息 |
| 清除会话 | DELETE | `/api/chat/sessions/{session_id}` | 可选 | P2 | 清除指定会话 |
| 文档上传 | POST | `/api/documents` | 可选 | P1 | 上传知识库文档 |
