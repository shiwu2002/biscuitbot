"""运行时模型预设选择的辅助函数集合。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责运行时模型预设
（model preset）选择与快照构建的辅助组件。
在项目架构中起到的作用：
- 提供一组无状态辅助函数，用于规范化预设名、聚合配置中的预设、构建预设快照
  （``ProviderSnapshot``）以及生成快照加载器；
- 被 ``AgentLoop`` 在初始化与运行时热更新模型/提供商时调用，实现“按预设名
  切换模型与生成参数”的能力，而无需重启 Agent；
- 兼容静态构建（直接从配置构造）与动态加载（通过 loader 回调）两种模式。
"""

from __future__ import annotations

from collections.abc import Callable  # 类型注解支持
from typing import Any  # 类型注解支持

from biscuitbot.config.schema import ModelPresetConfig  # 模型预设配置类型
from biscuitbot.providers.base import LLMProvider  # LLM 提供商基类
from biscuitbot.providers.factory import ProviderSnapshot, build_provider_snapshot  # 提供商快照及其构建函数

PresetSnapshotLoader = Callable[[str], ProviderSnapshot]  # 预设快照加载器类型：预设名 -> 快照


def default_selection_signature(signature: tuple[object, ...] | None) -> tuple[object, ...] | None:
    """从完整签名中提取“默认选择签名”（前两个元素），用于检测默认预设是否变化。"""
    return signature[:2] if signature else None


def configured_model_presets(config: Any) -> dict[str, ModelPresetConfig]:
    """聚合配置中的全部预设，并补充一个 ``default`` 条目。"""
    return {**config.model_presets, "default": config.resolve_default_preset()}


def make_preset_snapshot_loader(
    config: Any,
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None,
) -> PresetSnapshotLoader:
    """构造预设快照加载器。

    若提供了外部 ``provider_snapshot_loader``，则委托其按 ``preset_name`` 加载；
    否则使用 ``build_provider_snapshot`` 从配置直接构建。
    """
    if provider_snapshot_loader is not None:
        return lambda name: provider_snapshot_loader(preset_name=name)
    return lambda name: build_provider_snapshot(config, preset_name=name)


def build_static_preset_snapshot(
    provider: LLMProvider,
    name: str,
    preset: ModelPresetConfig,
) -> ProviderSnapshot:
    """静态构建预设快照：将预设的生成参数应用到 provider 并返回快照。

    参数:
        provider: LLM 提供商实例；
        name: 预设名；
        preset: 预设配置。

    返回:
        包含 provider、模型、上下文窗口与签名的 ``ProviderSnapshot``。
    """
    provider.generation = preset.to_generation_settings()
    return ProviderSnapshot(
        provider=provider,
        model=preset.model,
        context_window_tokens=preset.context_window_tokens,
        signature=("model_preset", name, preset.model_dump_json()),
    )


def build_runtime_preset_snapshot(
    *,
    name: str,
    presets: dict[str, ModelPresetConfig],
    provider: LLMProvider,
    loader: PresetSnapshotLoader | None,
) -> ProviderSnapshot:
    """运行时构建预设快照：优先使用 loader，否则回退到静态构建。

    参数:
        name: 预设名；
        presets: 全部预设字典；
        provider: LLM 提供商实例；
        loader: 可选的快照加载器。

    返回:
        对应预设的 ``ProviderSnapshot``。
    """
    if loader is not None:
        return loader(name)
    return build_static_preset_snapshot(provider, name, presets[name])


def normalize_preset_name(name: str | None, presets: dict[str, ModelPresetConfig]) -> str:
    """规范化并校验预设名。

    参数:
        name: 待校验的预设名；
        presets: 全部预设字典。

    返回:
        去除首尾空白后的合法预设名。

    异常:
        ValueError: 名称为空或非字符串；
        KeyError: 预设名不存在。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("model_preset must be a non-empty string")
    name = name.strip()
    if name not in presets:
        raise KeyError(f"model_preset {name!r} not found. Available: {', '.join(presets) or '(none)'}")
    return name

