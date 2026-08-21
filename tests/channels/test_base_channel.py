from types import SimpleNamespace

import pytest

from biscuitbot.bus.events import OutboundMessage
from biscuitbot.bus.queue import MessageBus
from biscuitbot.channels.base import BaseChannel
from biscuitbot.channels.wecom import WecomConfig


class _DummyChannel(BaseChannel):
    name = "dummy"
    _sent: list[OutboundMessage]

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self._sent = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self._sent.append(msg)


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


def test_is_allowed_supports_dict_allow_from_alias() -> None:
    channel = _DummyChannel({"allowFrom": ["alice"]}, MessageBus())

    assert channel.is_allowed("alice") is True


def test_is_allowed_denies_empty_dict_allow_from() -> None:
    channel = _DummyChannel({"allow_from": []}, MessageBus())

    assert channel.is_allowed("alice") is False


def test_is_allowed_handles_none_allow_from() -> None:
    channel = _DummyChannel({"allow_from": None}, MessageBus())
    assert channel.is_allowed("alice") is False

    channel2 = _DummyChannel({"allowFrom": None}, MessageBus())
    assert channel2.is_allowed("alice") is False


def test_is_allowed_star_allows_all() -> None:
    channel = _DummyChannel({"allowFrom": ["*"]}, MessageBus())
    assert channel.is_allowed("anyone") is True


def test_is_allowed_allow_all_dict() -> None:
    channel = _DummyChannel({"allow_all": True}, MessageBus())
    assert channel.is_allowed("anyone") is True
    assert channel.is_allowed("") is True


def test_is_allowed_allow_all_object() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_all=True), MessageBus())
    assert channel.is_allowed("anyone") is True


def test_is_allowed_allow_all_pydantic_config() -> None:
    """真实渠道配置（WecomConfig）必须保留 allow_all 字段。

    回归：Base 模型无 extra="allow"，model_validate 会静默丢弃未知字段，
    导致 is_allowed 永远读不到 allow_all，静默放行不生效、私聊仍发配对码。
    """
    config = WecomConfig.model_validate(
        {"enabled": True, "allow_all": True, "allowFrom": []}
    )
    channel = _DummyChannel(config, MessageBus())
    assert channel.is_allowed("anyone") is True
    assert channel.is_allowed("") is True


def test_is_allowed_allow_all_false_ignores() -> None:
    channel = _DummyChannel({"allow_all": False, "allowFrom": ["alice"]}, MessageBus())
    assert channel.is_allowed("alice") is True
    assert channel.is_allowed("stranger") is False


def test_is_allowed_pairing_fallback(monkeypatch) -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())
    monkeypatch.setattr(
        "biscuitbot.channels.base.is_approved", lambda _ch, sid: sid == "paired"
    )
    assert channel.is_allowed("paired") is True
    assert channel.is_allowed("unknown") is False


@pytest.mark.asyncio
async def test_handle_message_dm_sends_pairing_code(monkeypatch) -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())
    monkeypatch.setattr(
        "biscuitbot.channels.base.generate_code", lambda _ch, sid: "ABCD-EFGH"
    )

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=True
    )

    assert len(channel._sent) == 1
    msg = channel._sent[0]
    assert "ABCD-EFGH" in msg.content
    assert msg.metadata.get("_pairing_code") == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_handle_message_group_ignores_unknown() -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=False
    )

    assert channel._sent == []

