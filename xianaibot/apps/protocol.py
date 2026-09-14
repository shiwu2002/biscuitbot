"""Neutral manifest shape for settings-managed agent apps.

The manifest is intentionally descriptive. Installers still live in their
own adapters, while this protocol gives the WebUI and future registries one
small vocabulary for capabilities, trust, and verified install/remove plans.
"""

from __future__ import annotations

from typing import Any

APP_PROTOCOL_SCHEMA = "agent-app.v1"

# Unified capability schema.  Skills / CLI apps / MCP presets all map onto this
# one shape; ``runtime`` decides how the capability is executed, and install /
# requirements / provisioning become capability-wide concerns instead of being
# bound to a specific "app" type.
CAPABILITY_SCHEMA = "capability.v1"

# Runtime kinds.  ``workflow`` / ``agent`` / ``browser`` are reserved for future
# executors and are not implemented yet.
CAPABILITY_RUNTIME_TYPES = ("prompt", "process", "mcp", "workflow", "agent", "browser")


def compact_dict(values: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional values while preserving explicit booleans and zeros."""
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def app_manifest(
    *,
    app_id: str,
    display_name: str,
    description: str,
    category: str,
    source: str,
    capabilities: list[dict[str, Any]],
    install: dict[str, Any],
    remove: dict[str, Any],
    trust: dict[str, Any],
    version: str | None = None,
    logo_url: str | None = None,
    brand_color: str | None = None,
    docs_url: str | None = None,
) -> dict[str, Any]:
    """Build a stable app manifest dictionary."""
    return compact_dict({
        "schema": APP_PROTOCOL_SCHEMA,
        "id": app_id,
        "display_name": display_name,
        "version": version,
        "description": description,
        "category": category,
        "source": source,
        "logo_url": logo_url,
        "brand_color": brand_color,
        "docs_url": docs_url,
        "capabilities": capabilities,
        "install": install,
        "remove": remove,
        "trust": trust,
    })


def capability_manifest(
    *,
    capability_id: str,
    display_name: str,
    description: str,
    category: str,
    source: str,
    runtime: str,
    instructions: dict[str, Any],
    execution: dict[str, Any],
    requirements: dict[str, Any],
    provisioning: dict[str, Any],
    install: dict[str, Any],
    remove: dict[str, Any],
    trust: dict[str, Any],
    version: str | None = None,
    icon: str | None = None,
    tags: list[str] | None = None,
    logo_url: str | None = None,
    brand_color: str | None = None,
    docs_url: str | None = None,
) -> dict[str, Any]:
    """Build a unified capability manifest (``capability.v1``).

    A capability is a single unit of agent ability.  ``runtime`` decides how it
    runs (prompt-injected instructions, a local process, an MCP server, …),
    while ``instructions`` / ``execution`` / ``requirements`` / ``provisioning``
    describe its behaviour, dependencies and install story uniformly.
    """
    return compact_dict({
        "schema": CAPABILITY_SCHEMA,
        "id": capability_id,
        "display_name": display_name,
        "version": version,
        "description": description,
        "category": category,
        "source": source,
        "icon": icon,
        "tags": tags,
        "logo_url": logo_url,
        "brand_color": brand_color,
        "docs_url": docs_url,
        "runtime": runtime,
        "instructions": instructions,
        "execution": execution,
        "requirements": requirements,
        "provisioning": provisioning,
        "install": install,
        "remove": remove,
        "trust": trust,
    })
