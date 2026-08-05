"""Slash command routing and built-in handlers."""

from biscuitbot.command.builtin import register_builtin_commands
from biscuitbot.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
