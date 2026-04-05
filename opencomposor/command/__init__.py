"""Slash command routing and built-in handlers."""

from opencomposor.command.builtin import register_builtin_commands
from opencomposor.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
