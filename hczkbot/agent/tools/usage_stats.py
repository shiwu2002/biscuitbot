"""Tool usage statistics and cold-storage rotation.

Tracks how often each tool is called.  Tools that have not been invoked
for ``cold_storage_days`` days are rotated into cold storage — they are
removed from the INDEX.md and their schema is no longer sent to the model
(unless explicitly discovered via ``cold_storage`` → ``discover_tools``).

A call to any cold tool automatically recovers it back to the active index.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hczkbot.agent.tools.registry import ToolRegistry


@dataclass
class ToolUsageStat:
    """Per-tool call statistics."""

    call_count: int = 0
    last_called_at: float = 0.0  # unix timestamp; 0 = never called

    def record_call(self) -> None:
        self.call_count += 1
        self.last_called_at = time.time()


@dataclass
class ColdEntry:
    """A tool that has been rotated into cold storage."""

    name: str
    capability: str
    usage_md: str
    source_file: str = ""
    cold_since: float = 0.0


class UsageStats:
    """Tracks tool call frequency and manages cold storage rotation.

    Persists to ``usage_stats.json`` and ``cold_storage.json`` inside the
    ``.agent_tools/`` workspace directory.
    """

    def __init__(self, base_dir: Path | None = None):
        self._stats: dict[str, ToolUsageStat] = {}
        self._cold: dict[str, ColdEntry] = {}
        self._stats_file: Path | None = None
        self._cold_file: Path | None = None
        if base_dir is not None:
            base_dir.mkdir(parents=True, exist_ok=True)
            self._stats_file = base_dir / "usage_stats.json"
            self._cold_file = base_dir / "cold_storage.json"
        self._load()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_call(self, name: str) -> None:
        """Record a tool call.  Auto-recovers from cold storage."""
        stat = self._stats.setdefault(name, ToolUsageStat())
        stat.record_call()
        if name in self._cold:
            del self._cold[name]
        self._save()

    # ------------------------------------------------------------------
    # Cold storage queries
    # ------------------------------------------------------------------

    def is_cold(self, name: str) -> bool:
        return name in self._cold

    def cold_tool_names(self) -> list[str]:
        return list(self._cold.keys())

    def get_cold_entry(self, name: str) -> ColdEntry | None:
        return self._cold.get(name)

    def search_cold(self, query: str, limit: int = 10) -> list[ColdEntry]:
        """Search cold storage by keyword match on name + capability."""
        query_lower = query.lower()
        tokens = set(query_lower.split())
        scored: list[tuple[float, ColdEntry]] = []
        for entry in self._cold.values():
            text = f"{entry.name} {entry.capability}".lower()
            entry_tokens = set(text.split())
            overlap = len(tokens & entry_tokens)
            if overlap == 0:
                # Fallback: substring match
                if query_lower in text:
                    overlap = 1
                else:
                    continue
            scored.append((overlap, entry))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [e for _, e in scored[:limit]]

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def rotate_cold(
        self,
        registry: "ToolRegistry",
        threshold_days: int,
    ) -> list[str]:
        """Move tools not called in *threshold_days* to cold storage.

        Always-include tools are never rotated (they are core infrastructure).
        Tools that have never been called are only rotated if they have been
        registered for longer than the threshold (using a heuristic: the stat
        must exist with ``last_called_at == 0`` AND the tool is not newly
        registered — approximated by checking if it has been in stats for at
        least one rotation cycle).

        Returns the list of newly-cold tool names.
        """
        if threshold_days <= 0:
            return []
        now = time.time()
        threshold_s = threshold_days * 86400
        newly_cold: list[str] = []
        for name in registry.tool_names:
            # Never rotate always-include tools
            if registry._tool_is_always_include(name):
                continue
            if name in self._cold:
                continue
            stat = self._stats.get(name)
            if stat is None:
                # Tool never tracked — skip (likely just registered)
                continue
            if stat.last_called_at == 0:
                # Never called — skip for now (give it a chance)
                continue
            if now - stat.last_called_at > threshold_s:
                tool = registry.get(name)
                if tool is None:
                    continue
                self._cold[name] = ColdEntry(
                    name=name,
                    capability=tool.capability,
                    usage_md=getattr(tool, "_usage_md", ""),
                    source_file=getattr(tool.__class__, "__module__", ""),
                    cold_since=now,
                )
                newly_cold.append(name)
        if newly_cold:
            self._save()
        return newly_cold

    def restore(self, name: str) -> bool:
        """Manually restore a cold tool.  Returns True if it was cold."""
        if name in self._cold:
            del self._cold[name]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._stats_file is not None:
            data = {k: asdict(v) for k, v in self._stats.items()}
            self._stats_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if self._cold_file is not None:
            data = {k: asdict(v) for k, v in self._cold.items()}
            self._cold_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _load(self) -> None:
        if self._stats_file is not None and self._stats_file.is_file():
            try:
                data = json.loads(self._stats_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    self._stats[k] = ToolUsageStat(**v)
            except (json.JSONDecodeError, TypeError):
                pass
        if self._cold_file is not None and self._cold_file.is_file():
            try:
                data = json.loads(self._cold_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    self._cold[k] = ColdEntry(**v)
            except (json.JSONDecodeError, TypeError):
                pass
