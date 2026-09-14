"""Configurable prompt-injection / shell-interception guard levels.

Centralises the level → feature mapping so the rest of the codebase has a
single truth source.  ``GuardPolicy`` is a frozen dataclass; construct it from
``ToolsConfig.guard_level`` and read the boolean properties.

Structural access controls (SSRF network isolation, workspace path boundary,
environment-variable allowlist, subagent isolation, runtime-context tagging)
are intentionally *not* gated by ``guard_level`` — they are not prompt-injection
defences and disabling them would cause catastrophic damage unrelated to
injection flexibility.
"""

from __future__ import annotations

from dataclasses import dataclass

STANDARD = "standard"
MINIMAL = "minimal"
OFF = "off"

#: Valid level names.  Anything else falls back to ``STANDARD``.
VALID_LEVELS: tuple[str, ...] = (STANDARD, MINIMAL, OFF)


def normalize_guard_level(value: str | None) -> str:
    """Lowercase and validate *value*; return ``STANDARD`` for anything unknown."""
    if not value:
        return STANDARD
    level = value.strip().lower()
    return level if level in VALID_LEVELS else STANDARD


@dataclass(frozen=True)
class GuardPolicy:
    """Level → feature mapping for the configurable guard.

    Properties:
        shell_denylist: full hardcoded deny-list (catastrophic + friction
            patterns such as download-and-execute and internal-state-file
            protection).  Only at ``STANDARD``.
        catastrophic_shell_blocks: catastrophic commands only (rm -rf, mkfs,
            dd to device, fork bomb, shutdown).  ``STANDARD`` and ``MINIMAL``.
        untrusted_banner: ``web_fetch`` prepends the ``[External content …]``
            banner.  Only at ``STANDARD``.
        untrusted_snippet: system-prompt includes the untrusted-content snippet.
            ``STANDARD`` and ``MINIMAL``.
    """

    level: str = STANDARD

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", normalize_guard_level(self.level))

    @property
    def shell_denylist(self) -> bool:
        return self.level == STANDARD

    @property
    def catastrophic_shell_blocks(self) -> bool:
        return self.level in (STANDARD, MINIMAL)

    @property
    def untrusted_banner(self) -> bool:
        return self.level == STANDARD

    @property
    def untrusted_snippet(self) -> bool:
        return self.level in (STANDARD, MINIMAL)
