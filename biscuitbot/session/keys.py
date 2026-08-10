"""共享的会话键常量与辅助函数。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录，提供会话键（session key）的统一生成规则。
在项目架构中起到的作用：根据渠道与聊天 ID 生成会话唯一标识，支持统一会话模式
（跨渠道共享同一会话），是会话管理器定位会话的依据。
"""

from __future__ import annotations

# 统一会话模式下的固定会话键（跨所有渠道共享）
UNIFIED_SESSION_KEY = "unified:default"


def session_key_for_channel(channel: str, chat_id: str, *, unified_session: bool = False) -> str:
    """返回渠道/聊天对对应的会话键。

    unified_session 为 True 时返回统一的默认会话键，否则按 ``channel:chat_id`` 拼接。
    """
    if unified_session:
        return UNIFIED_SESSION_KEY
    return f"{channel}:{chat_id}"
