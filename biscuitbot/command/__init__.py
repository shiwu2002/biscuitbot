"""Slash command routing and built-in handlers."""

from hczkbot.command.builtin import register_builtin_commands
from hczkbot.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
