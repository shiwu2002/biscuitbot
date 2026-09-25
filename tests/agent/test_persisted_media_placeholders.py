"""P0：多模态 base64 绝不能落进 session 历史。

``_sanitize_persisted_blocks`` 决定哪些内容块在写入 session JSONL 之前被
替换为占位文本。图片内联 base64 可达数 MB，漏掉就会被永久写进会话文件，
并在之后**每一轮**回放时重新塞进上下文窗口——会话膨胀 + 上下文必然爆掉。
路径必须留在占位文本里，模型之后还能靠它去读文件。

（视频不在此列：它从来不构造内容块，进历史的只有路径说明，见
``agent/context.py::_build_user_content``。）
"""

from __future__ import annotations

from xianaibot.agent.loop import AgentLoop


def _sanitize(content: list[dict]) -> list[dict]:
    dummy = AgentLoop.__new__(AgentLoop)
    return AgentLoop._sanitize_persisted_blocks(dummy, content)


def test_inline_image_becomes_placeholder() -> None:
    out = _sanitize([
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "_meta": {"path": "/media/ab12.png"},
        }
    ])
    assert out == [{"type": "text", "text": "[image: /media/ab12.png]"}]


def test_no_data_payload_survives_sanitization() -> None:
    """整个产出里不能残留任何 ``data:`` 字节。"""
    blocks = [
        {"type": "text", "text": "看看这些"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "A" * 4096},
            "_meta": {"path": "/media/big.png"},
        },
    ]

    out = _sanitize(blocks)

    for block in out:
        assert "data:image/" not in str(block)
    assert out[0] == {"type": "text", "text": "看看这些"}


def test_remote_image_url_block_is_preserved() -> None:
    """纯 URL 源的图片块不含 base64、不占体积，保留它回放才能看到原链接。"""
    block = {"type": "image_url", "image_url": {"url": "https://cdn.example.com/a.png"}}

    assert _sanitize([block]) == [block]


def test_image_block_without_meta_path_falls_back_to_generic_placeholder() -> None:
    out = _sanitize([
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    ])
    assert out == [{"type": "text", "text": "[image]"}]
