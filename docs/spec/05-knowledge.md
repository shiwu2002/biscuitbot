# 5. 知识库入库与检索

## 5.1 入库流程

```
文件上传
  ↓
DocumentParser.parseWithPages() — 逐页解析
  ├── PDF: 逐页文本提取
  │   ├── 正常 PDF → 保留页码、章节
  │   └── 扫描件（字符/页 < 阈值）→ PDFRenderer 渲染 → GLM-OCR 识别
  ├── DOCX: 提取文本 + 内嵌图片 + 表格结构
  ├── TXT/MD: 直接读取
  └── XLSX: QA 结构化提取
  ↓
章节检测 + 跨页表格检测 + 表格延续标记
  ↓
LlmPreprocessor.summarizeAndSplitWithPages() — 页感知预处理
  ├── 合并跨页表格（延续页合并为完整段）
  ├── 构建篇章上下文 prompt（章节+页码+源文件）
  └── LLM 总结切割 → SemanticChunk（带溯源信息）
  ↓
Ingester.ingestPages() → ingestChunks()
  └── 写入 Milvus：content + 溯源字段
```

## 5.2 OCR 识别

平台集成 GLM-OCR 模型，自动检测扫描件并识别：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `knowledge.ocr.enabled` | `false` | OCR 开关 |
| `knowledge.ocr.model-id` | — | GLM-OCR 对应的 AiModel ID |
| `knowledge.ocr.scan-threshold` | `50` | 扫描件检测阈值（平均字符/页 < 阈值则走 OCR） |
| `knowledge.ocr.render-dpi` | `150` | PDF 渲染 DPI |

**扫描件自动检测**：PDFTextStripper 提取的字符/页 < 阈值时自动走 OCR，无需手动指定。

## 5.3 LLM 预处理

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `knowledge.llm-preprocess.enabled` | `false` | 预处理开关 |
| `knowledge.llm-preprocess.model-id` | — | 预处理用的 AiModel ID |
| `knowledge.llm-preprocess.min-text-length` | `200` | 触发预处理的最小文本长度 |
| `knowledge.llm-preprocess.target-chunk-size` | `500` | 每个语义块目标长度 |

**关键能力**：
- 跨页表格合并：检测到表格延续标记后，合并多页表格为一个完整段
- 篇章上下文感知：prompt 中携带章节和页码信息，模型根据上下文进行语义切割
- 安全回退：LLM 调用失败自动回退到规则分块

## 5.4 Milvus Schema（溯源字段）

| 字段 | 类型 | 说明 |
|------|------|------|
| `page_number` | Int64 | 原始文档页码 |
| `chapter` | VarChar(256) | 所属章节 |
| `context_pages` | VarChar(64) | 关联上下文页码范围 |
| `table_html` | VarChar(65535) | 表格 HTML 结构 |
| `source_filename` | VarChar(512) | 源文件名 |
| `chunk_type` | VarChar(16) | 块类型：prose/qa/image/table |

## 5.5 检索结果溯源

检索结果中每个 chunk 包含以下溯源信息：

```json
{
  "content": "文本内容或图片描述",
  "score": 0.85,
  "source": "product_manual.pdf",
  "metadata": {
    "chunkType": "prose",
    "pageNumber": 3,
    "chapter": "第二章 产品定价",
    "contextPages": "2-4",
    "sourceFilename": "product_manual.pdf",
    "tableHtml": null
  }
}
```

智能体可在回复中引用出处，例如："根据《产品手册》第3页第二章的描述..."
