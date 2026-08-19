"""知识库检索工具：让 agent 在用户上传的个人知识库中检索资料。

上传时已在 ``workspace/knowledge_index.db`` 建好 FTS5 全文索引（中文按二元
分词），本工具读取该索引返回命中文档的文件名与预览，agent 再按需用
``read_file("knowledge/<文件名>")`` 读取原文。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.utils.knowledge_index import search_knowledge  # 知识库检索


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "检索关键词或短语，用于在个人知识库中查找相关文档/图片",
        },
        "limit": {
            "type": "integer",
            "description": "最大返回数量（默认 5，上限 20）",
        },
    },
    "required": ["query"],
})
class SearchKnowledgeTool(Tool):
    """在个人知识库中检索用户上传的文档与图片。

    职责：根据查询词返回知识库中最相关的文档/图片的文件名与内容预览，
    agent 随后用 read_file 读取 ``knowledge/<文件名>`` 获取全文。

    用法：由 agent 在需要查找用户上传资料时调用。
    """

    _capability = (
        "Search the user's personal knowledge base (uploaded documents/images) "
        "by keyword; returns matching filenames and previews to read with read_file."
    )
    _always_include = True  # 知识库是核心功能，schema 始终发送给模型

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or Path(".")

    @classmethod
    def create(cls, ctx: Any) -> "SearchKnowledgeTool":
        return cls(workspace=Path(ctx.workspace))

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "Search the user's personal knowledge base for uploaded documents and "
            "images. Returns matching filenames with a short preview; read the full "
            "content with read_file(\"knowledge/<filename>\")."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return json.dumps(
                {"error": "缺少检索关键词 query，请提供要检索的词语"}, ensure_ascii=False
            )

        limit = kwargs.get("limit") or 5
        if not isinstance(limit, int) or limit < 1:
            return json.dumps(
                {"error": f"limit 必须是 1-20 之间的整数，收到 {limit!r}"},
                ensure_ascii=False,
            )
        limit = min(limit, 20)

        results = search_knowledge(self._workspace, query, limit=limit)
        if not results:
            return json.dumps(
                {
                    "message": f"知识库中没有找到与「{query}」相关的内容。",
                    "hint": "可以尝试更短或更通用的关键词。",
                    "results": [],
                },
                ensure_ascii=False,
            )

        rows = [
            {
                "file": f"knowledge/{item['doc_id']}",
                "kind": item["kind"],
                "caption": item["caption"],
                "preview": item["preview"],
                "size": item["size"],
            }
            for item in results
        ]
        return json.dumps(
            {
                "message": f"找到 {len(rows)} 条相关知识库内容。",
                "hint": "用 read_file 读取命中文件的完整内容。",
                "results": rows,
            },
            ensure_ascii=False,
        )
