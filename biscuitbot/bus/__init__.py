"""Message bus module for decoupled channel-agent communication."""

from biscuitbot.bus.events import InboundMessage, OutboundMessage
from biscuitbot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
