"""CLI app adapter for the unified Apps domain."""

from biscuitbot.apps.cli.service import (
    CliAppError,
    CliAppManager,
    CliAppsRuntimeConfig,
)

__all__ = [
    "CliAppError",
    "CliAppManager",
    "CliAppsRuntimeConfig",
]
