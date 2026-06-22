"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
from pydantic import BaseModel

from hczkbot.config.schema import Config, _resolve_tool_config_refs

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False

# 支持的配置文件扩展名（按优先级排序）
_YAML_SUFFIXES = {".yaml", ".yml"}
_JSON_SUFFIXES = {".json"}


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path.

    优先级：已设置的路径 > ~/.hczkbot/config.yaml > ~/.hczkbot/config.yml > ~/.hczkbot/config.json
    """
    if _current_config_path:
        return _current_config_path
    home_hczkbot = Path.home() / ".hczkbot"
    for name in ("config.yaml", "config.yml", "config.json"):
        candidate = home_hczkbot / name
        if candidate.exists():
            return candidate
    return home_hczkbot / "config.yaml"


def _load_config_file(path: Path) -> dict[str, Any]:
    """根据文件扩展名加载配置文件（支持 YAML 和 JSON）。

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

    suffix = path.suffix.lower()
    try:
        with open(path, encoding="utf-8") as f:
            if suffix in _YAML_SUFFIXES:
                import yaml

                data = yaml.safe_load(f)
            elif suffix in _JSON_SUFFIXES:
                data = json.load(f)
            else:
                # 默认尝试 YAML（兼容无扩展名或未知扩展名）
                import yaml

                data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse YAML config from {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON config from {path}: {e}") from e

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file must contain a mapping at top level, got {type(data).__name__}: {path}"
        )
    return data


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    支持 YAML（.yaml/.yml）和 JSON（.json）格式，根据文件扩展名自动判断。

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

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
    from hczkbot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    根据文件扩展名自动选择 YAML 或 JSON 格式保存。

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    suffix = path.suffix.lower()
    with open(path, "w", encoding="utf-8") as f:
        if suffix in _YAML_SUFFIXES:
            import yaml

            yaml.safe_dump(
                data, f, indent=2, allow_unicode=True, sort_keys=False, default_flow_style=False
            )
        else:
            json.dump(data, f, indent=2, ensure_ascii=False)


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
    if isinstance(obj, BaseModel):
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
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
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

    return data
