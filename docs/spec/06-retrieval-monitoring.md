# 6. 检索监控

## 6.1 概述

平台自动记录每次知识库检索的详细信息，包括命中率、耗时、策略、溯源摘要等，供管理员监控检索质量和优化知识库。

## 6.2 监控数据

### 检索日志（retrieval_logs）

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_id` | VARCHAR(64) | 智能体ID（用户雪花ID） |
| `collection_name` | VARCHAR(128) | 集合名 |
| `query` | TEXT | 检索查询文本 |
| `strategy` | VARCHAR(16) | 检索策略：hybrid/vector/keyword/fallback |
| `hit_success` | TINYINT(1) | 是否命中有效结果 |
| `hit_count` | INT | 命中结果数量 |
| `top_k` | INT | 请求的 topK |
| `top_score` | DOUBLE | 最高得分 |
| `min_score` | DOUBLE | 最低得分 |
| `avg_score` | DOUBLE | 平均得分 |
| `confidence` | DOUBLE | 置信度 |
| `fallback_used` | TINYINT(1) | 是否降级到兜底回复 |
| `hit_sources` | TEXT | 命中块溯源摘要 JSON |
| `chunk_type_dist` | VARCHAR(512) | 命中块类型分布 JSON |
| `avg_distance` | DOUBLE | 平均向量距离 |
| `adopted` | TINYINT(1) | 用户采纳反馈（NULL=未反馈/1=采纳/0=拒绝） |
| `duration_ms` | BIGINT | 检索耗时（毫秒） |

### 入库日志（ingest_logs）

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_id` | VARCHAR(64) | 智能体ID |
| `collection_name` | VARCHAR(128) | 集合名 |
| `source` | VARCHAR(512) | 来源标识（文件名等） |
| `file_type` | VARCHAR(16) | 文件类型 |
| `total_pages` | INT | 文档总页数 |
| `total_chunks` | INT | 入库分块数 |
| `ocr_used` | TINYINT(1) | 是否使用了 OCR |
| `llm_preprocess_used` | TINYINT(1) | 是否使用了 LLM 预处理 |
| `llm_preprocess_ms` | BIGINT | LLM 预处理耗时 |
| `ingest_duration_ms` | BIGINT | 入库总耗时 |
| `status` | VARCHAR(16) | 状态：success/error |

### 服务健康指标（service_health_metrics）

| 字段 | 类型 | 说明 |
|------|------|------|
| `service_name` | VARCHAR(64) | 服务名：embedder/milvus/ocr/llm_preprocess |
| `operation` | VARCHAR(64) | 操作名 |
| `success` | TINYINT(1) | 是否成功 |
| `duration_ms` | BIGINT | 耗时 |
| `error_type` | VARCHAR(128) | 错误类型 |

## 6.3 监控 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/retrieval-logs` | GET | 分页查询检索记录 |
| `/api/retrieval-logs/stats` | GET | 统计概览 |
| `/api/retrieval-logs/trend` | GET | 命中率趋势（按天聚合） |
| `/api/retrieval-logs/{id}` | GET | 单条详情 |
| `/api/retrieval-logs/{id}/adopt` | POST | 采纳/拒绝反馈 |
| `/api/retrieval-logs/agent-ranking` | GET | 智能体维度排行 |
| `/api/retrieval-logs/alerts` | GET | 异常告警 |
| `/api/retrieval-logs/ingest-logs` | GET | 入库操作日志 |
| `/api/retrieval-logs/service-health` | GET | 服务健康指标 |

## 6.4 用户采纳反馈

智能体或前端可对检索结果进行采纳/拒绝反馈，用于评估检索质量：

```
POST /api/retrieval-logs/{id}/adopt
Content-Type: application/json

{
  "adopted": true   // true=采纳, false=拒绝
}
```
