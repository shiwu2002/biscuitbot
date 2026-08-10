"""配置加载工具集。

所属模块与项目作用
===================
本文件位于 biscuitbot/config 目录，提供 biscuitbot 配置文件的加载、保存与迁移工具。
在项目架构中起到的作用：负责从 JSON 配置文件（默认 ~/.biscuitbot/config.json）读取
配置、自动迁移旧版 YAML/字段格式、解析 ``${VAR}`` 环境变量引用，并应用 SSRF 白名单
等安全策略，最终返回 Pydantic Config 对象供全局使用。同时支持多实例场景下的配置路径
覆盖。
"""

import json  # JSON 配置文件读写
import os  # 读取环境变量用于配置引用解析
import re  # 匹配 ${VAR} 环境变量引用
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 任意类型标注

import pydantic  # 配置校验与异常类型

from biscuitbot.config.schema import Config, _resolve_tool_config_refs  # 配置数据模型与前置引用解析

# 全局变量：当前配置文件路径（用于多实例支持）
_current_config_path: Path | None = None
# 标记 schema 的工具配置前置引用是否已解析（避免重复 rebuild）
_schema_refs_ready = False


def set_config_path(path: Path) -> None:
    """设置当前配置文件路径（用于派生数据目录，支持多实例）。"""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """获取配置文件路径。

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

    # 空文件视为空字典
    if data is None:
        return {}
    # 顶层必须是映射对象
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file must contain a mapping at top level, got {type(data).__name__}: {path}"
        )
    return data


def _migrate_yaml_to_json() -> None:
    """当不存在 JSON 配置时，自动将遗留的 YAML 配置迁移为 JSON。"""
    home_biscuitbot = Path.home() / ".biscuitbot"
    json_path = home_biscuitbot / "config.json"
    # 已有 JSON 配置则无需迁移
    if json_path.exists():
        return
    # 依次尝试 config.yaml 与 config.yml
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
    """从文件加载配置，或创建默认配置。

    仅支持 JSON（.json）格式。首次加载时自动迁移旧的 YAML 配置。

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    # 首次加载时解析工具配置的前置引用，确保 schema 已 rebuild
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

    _migrate_yaml_to_json()
    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            data = _load_config_file(path)
            data = _migrate_config(data)  # 迁移旧字段格式
            config = Config.model_validate(data)
        except (FileNotFoundError, ValueError, pydantic.ValidationError) as e:
            raise ValueError(f"Failed to load config from {path}: {e}") from e

    _apply_ssrf_whitelist(config)  # 应用 SSRF 白名单到网络安全模块
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """将配置中的 SSRF 白名单应用到网络安全模块。"""
    from biscuitbot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """将配置保存到文件（JSON 格式）。

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # 以 JSON 模式导出，保留别名以兼容既有配置文件键名
    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # 限制配置文件权限：文件内含提供商 API Key 与渠道密钥。
    # 尽力设置为 0o600（Windows 上为空操作）。
    try:
        path.chmod(0o600)
    except OSError:
        pass


# 匹配 ${VAR} 形式的环境变量引用
_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """返回解析了 ``${VAR}`` 环境变量引用后的配置。

    原地遍历以保证声明了 ``exclude=True`` 的字段得以保留；若无引用则返回同一实例。
    若引用的变量未设置则抛出 ``ValueError``。
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    """原地递归解析对象中的 ``${VAR}`` 引用，返回新对象（无变化时返回原对象）。"""
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, pydantic.BaseModel):
        # 遍历模型字段，收集需要更新的字段
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        # 处理额外字段（extras）
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
    """在纯字符串/字典/列表中递归解析 ``${VAR}`` 模式。"""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    """正则替换回调：返回环境变量值，未设置则抛出 ValueError。"""
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


def _migrate_config(data: dict) -> dict:
    """将旧版配置格式迁移为当前格式。"""
    # 警告已移除的 SDK 提供商后端。这些 SDK 提供商已被移除，
    # 改为通过 OpenAI 兼容的 apiBase 访问相同模型。仍带这些键的旧配置
    # 会被忽略并给出警告。
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
            # 移除已废弃的提供商键
            for k in stale:
                providers.pop(k, None)

    # 将 tools.exec.restrictToWorkspace 迁移到 tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # 将 tools.myEnabled / tools.mySet 迁移到 tools.my.{enable, allowSet}
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

    # 平台集成模块已移除。静默丢弃遗留配置中的 `platform` 键，使既有
    # ~/.biscuitbot/config.json 文件能继续加载，而非因 pydantic 的 extra-forbidden
    # 校验而失败。
    if "platform" in data:
        data.pop("platform", None)

    return data
