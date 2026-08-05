"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic

from biscuitbot.config.schema import Config, _resolve_tool_config_refs

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path.

    优先级：已设置的路径 > ~/.biscuitbot/config.json
    """
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".biscuitbot" / "config.json"


def _load_config_file(path: Path) -> dict[str, Any]:
    """加载 JSON 配置文件。

    Args:
        path: 配置文件路径

    Returns:
        解析后的配置字典

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件格式不支持或解析失败
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON config from {path}: {e}") from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file must contain a mapping at top level, got {type(data).__name__}: {path}"
        )
    return data


def _migrate_yaml_to_json() -> None:
    """Auto-migrate legacy YAML config to JSON if no JSON config exists."""
    home_biscuitbot = Path.home() / ".biscuitbot"
    json_path = home_biscuitbot / "config.json"
    if json_path.exists():
        return
    for yaml_name in ("config.yaml", "config.yml"):
        yaml_path = home_biscuitbot / yaml_name
        if yaml_path.exists():
            try:
                import yaml as _yaml

                with open(yaml_path, encoding="utf-8") as f:
                    data = _yaml.safe_load(f)
                if isinstance(data, dict):
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    from loguru import logger
                    logger.info("Auto-migrated {} -> {}", yaml_path, json_path)
            except Exception:
                from loguru import logger
                logger.warning("Failed to auto-migrate YAML config {} -> {}", yaml_path, json_path, exc_info=True)
            break


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    仅支持 JSON（.json）格式。首次加载时自动迁移旧的 YAML 配置。

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

    _migrate_yaml_to_json()
    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            data = _load_config_file(path)
            data = _migrate_config(data)
            config = Config.model_validate(data)
        except (FileNotFoundError, ValueError, pydantic.ValidationError) as e:
            raise ValueError(f"Failed to load config from {path}: {e}") from e

    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from biscuitbot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file (JSON format).

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Restrict permissions on the config file: it contains provider API keys
    # and channel secrets. Best-effort chmod 0o600 (no-op on Windows).
    try:
        path.chmod(0o600)
    except OSError:
        pass


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` survive;
    returns the same instance when no references are present.
    Raises ``ValueError`` if a referenced variable is not set.
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, pydantic.BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Warn about removed provider backends. These SDK providers were removed in
    # favor of accessing the same models via OpenAI-compatible apiBase. Old
    # configs that still carry these keys will be ignored with a warning.
    _removed_provider_keys = {
        "azure_openai",
        "bedrock",
        "openai_codex",
        "github_copilot",
    }
    providers = data.get("providers", {})
    if isinstance(providers, dict):
        stale = [k for k in _removed_provider_keys if k in providers]
        if stale:
            import warnings

            warnings.warn(
                "Config providers."
                + "/".join(stale)
                + " are no longer supported (SDK providers removed). "
                "Use an OpenAI-compatible provider with apiBase/apiKey/model "
                "to access these models. The stale keys will be ignored.",
                DeprecationWarning,
                stacklevel=2,
            )
            for k in stale:
                providers.pop(k, None)

    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    # The platform integration module was removed. Silently drop any leftover
    # `platform` key from legacy configs so existing ~/.biscuitbot/config.json files
    # keep loading instead of failing pydantic's extra-forbidden validation.
    if "platform" in data:
        data.pop("platform", None)

    return data
