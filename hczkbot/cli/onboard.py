"""hczkbot 交互式配置引导问卷。"""

import json
import types
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, NamedTuple, get_args, get_origin

try:
    import questionary
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without wizard deps
    questionary = None
from loguru import logger
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hczkbot.cli.models import (
    format_token_count,
    get_model_context_limit,
    get_model_suggestions,
)
from hczkbot.config.loader import get_config_path, load_config
from hczkbot.config.schema import Config, ModelPresetConfig

console = Console()


@dataclass
class OnboardResult:
    """引导会话的返回结果。"""

    config: Config
    should_save: bool

# --- Field Hints for Select Fields ---
# Maps field names to (choices, hint_text)
# To add a new select field with hints, add an entry:
#   "field_name": (["choice1", "choice2", ...], "hint text for the field")
_SELECT_FIELD_HINTS: dict[str, tuple[list[str], str]] = {
    "reasoning_effort": (
        ["low", "medium", "high"],
        "low / medium / high - enables LLM thinking mode",
    ),
}

# --- Key Bindings for Navigation ---

_BACK_PRESSED = object()  # Sentinel value for back navigation

# Cache of model-preset names populated at runtime so that field handlers can
# offer existing presets as choices (e.g. AgentDefaults.model_preset).
_MODEL_PRESET_CACHE: set[str] = set()


def _get_questionary():
    """返回 questionary，若引导依赖未安装则抛出明确错误。"""
    if questionary is None:
        raise RuntimeError(
            "Interactive onboarding requires the optional 'questionary' dependency. "
            "Install project dependencies and rerun with --wizard."
        )
    return questionary


def _select_with_back(
    prompt: str, choices: list[str], default: str | None = None
) -> str | None | object:
    """支持 Escape/左方向键返回的选择菜单。

    Args:
        prompt: 显示的提示文本。
        choices: 可选项列表，不能为空。
        default: 默认预选的选项；若不在 choices 中则使用第一项。

    Returns:
        _BACK_PRESSED 哨兵值：用户按 Escape 或左方向键
        选中选项字符串：用户确认选择
        None：用户取消（Ctrl+C）
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    # 校验 choices
    if not choices:
        logger.warning("_select_with_back 收到空 choices 列表")
        return None

    # 查找默认索引
    selected_index = 0
    if default and default in choices:
        selected_index = choices.index(default)

    # 保存结果的状态容器
    state: dict[str, str | None | object] = {"result": None}

    # 构建菜单项（通过闭包引用 selected_index）
    def get_menu_text():
        items = []
        for i, choice in enumerate(choices):
            if i == selected_index:
                items.append(("class:selected", f"> {choice}\n"))
            else:
                items.append(("", f"  {choice}\n"))
        return items

    # 创建布局
    menu_control = FormattedTextControl(get_menu_text)
    menu_window = Window(content=menu_control, height=len(choices))

    prompt_control = FormattedTextControl(lambda: [("class:question", f"> {prompt}")])
    prompt_window = Window(content=prompt_control, height=1)

    layout = Layout(HSplit([prompt_window, menu_window]))

    # 键位绑定
    bindings = KeyBindings()

    @bindings.add(Keys.Up)
    def _up(event):
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(choices)
        event.app.invalidate()

    @bindings.add(Keys.Down)
    def _down(event):
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(choices)
        event.app.invalidate()

    @bindings.add(Keys.Enter)
    def _enter(event):
        state["result"] = choices[selected_index]
        event.app.exit()

    @bindings.add("escape")
    def _escape(event):
        state["result"] = _BACK_PRESSED
        event.app.exit()

    @bindings.add(Keys.Left)
    def _left(event):
        state["result"] = _BACK_PRESSED
        event.app.exit()

    @bindings.add(Keys.ControlC)
    def _ctrl_c(event):
        state["result"] = None
        event.app.exit()

    # 样式
    style = Style.from_dict({
        "selected": "fg:green bold",
        "question": "fg:cyan",
    })

    app = Application(layout=layout, key_bindings=bindings, style=style)
    try:
        app.run()
    except Exception:
        logger.exception("选择提示符运行出错")
        return None

    return state["result"]

# --- Type Introspection ---


class FieldTypeInfo(NamedTuple):
    """字段类型内省结果。"""

    type_name: str
    inner_type: Any


def _get_field_type_info(field_info) -> FieldTypeInfo:
    """从 Pydantic 字段中提取类型信息。"""
    annotation = field_info.annotation
    if annotation is None:
        return FieldTypeInfo("str", None)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is types.UnionType:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            annotation = non_none_args[0]
            origin = get_origin(annotation)
            args = get_args(annotation)

    _simple_types: dict[type, str] = {bool: "bool", int: "int", float: "float"}

    if origin is list or (hasattr(origin, "__name__") and origin.__name__ == "List"):
        return FieldTypeInfo("list", args[0] if args else str)
    if origin is dict or (hasattr(origin, "__name__") and origin.__name__ == "Dict"):
        return FieldTypeInfo("dict", None)
    for py_type, name in _simple_types.items():
        if annotation is py_type:
            return FieldTypeInfo(name, None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return FieldTypeInfo("model", annotation)
    if origin is Literal:
        return FieldTypeInfo("literal", list(args))
    return FieldTypeInfo("str", None)


def _get_field_display_name(field_key: str, field_info) -> str:
    """获取字段显示名。"""
    if field_info and field_info.description:
        return field_info.description
    name = field_key
    suffix_map = {
        "_s": " (seconds)",
        "_ms": " (ms)",
        "_url": " URL",
        "_path": " Path",
        "_id": " ID",
        "_key": " Key",
        "_token": " Token",
    }
    for suffix, replacement in suffix_map.items():
        if name.endswith(suffix):
            name = name[: -len(suffix)] + replacement
            break
    return name.replace("_", " ").title()


# --- Sensitive Field Masking ---

_SENSITIVE_KEYWORDS = frozenset({"api_key", "token", "secret", "password", "credentials"})


def _is_sensitive_field(field_name: str) -> bool:
    """判断字段名是否表示敏感内容。"""
    return any(kw in field_name.lower() for kw in _SENSITIVE_KEYWORDS)


def _mask_value(value: str) -> str:
    """对敏感值打码，仅显示最后 4 个字符。"""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


# --- Value Formatting ---


def _format_value(value: Any, rich: bool = True, field_name: str = "") -> str:
    """递归安全展示任意深度值的统一入口。"""
    if value is None or value == "" or value == {} or value == []:
        return "[dim]not set[/dim]" if rich else "[not set]"
    if _is_sensitive_field(field_name) and isinstance(value, str):
        masked = _mask_value(value)
        return f"[dim]{masked}[/dim]" if rich else masked
    if isinstance(value, BaseModel):
        parts = []
        for fname, _finfo in type(value).model_fields.items():
            fval = getattr(value, fname, None)
            formatted = _format_value(fval, rich=False, field_name=fname)
            if formatted != "[not set]":
                parts.append(f"{fname}={formatted}")
        return ", ".join(parts) if parts else ("[dim]not set[/dim]" if rich else "[not set]")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        # 处理包含 BaseModel 实例的 dict
        parts = []
        for k, v in value.items():
            formatted = _format_value(v, rich=False, field_name=str(k))
            parts.append(f"{k}: {formatted}")
        return ", ".join(parts) if parts else ("[dim]not set[/dim]" if rich else "[not set]")
    return str(value)


def _format_value_for_input(value: Any, field_type: str) -> str:
    """将值格式化为输入框的默认值。"""
    if value is None or value == "":
        return ""
    if field_type == "list" and isinstance(value, list):
        return ",".join(str(v) for v in value)
    if field_type == "dict" and isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _validate_field_constraint(value: Any, field_info) -> str | None:
    """按 Pydantic Field 约束校验值。

    校验失败时返回错误信息字符串，校验通过返回 None。
    使用基于属性的检测以适配 Pydantic v2 内部类型。
    """
    if field_info is None or not hasattr(field_info, "metadata"):
        return None

    for m in field_info.metadata:
        if hasattr(m, "ge") and isinstance(value, (int, float)):
            if value < m.ge:
                return f"值必须 >= {m.ge}"
        if hasattr(m, "gt") and isinstance(value, (int, float)):
            if value <= m.gt:
                return f"值必须 > {m.gt}"
        if hasattr(m, "le") and isinstance(value, (int, float)):
            if value > m.le:
                return f"值必须 <= {m.le}"
        if hasattr(m, "lt") and isinstance(value, (int, float)):
            if value >= m.lt:
                return f"值必须 < {m.lt}"
        if hasattr(m, "min_length") and hasattr(value, "__len__"):
            if len(value) < m.min_length:
                return f"长度必须 >= {m.min_length}"
        if hasattr(m, "max_length") and hasattr(value, "__len__"):
            if len(value) > m.max_length:
                return f"长度必须 <= {m.max_length}"

    return None


def _get_constraint_hint(field_info) -> str:
    """从字段元数据中推导人类可读的约束提示。

    返回类似 "(0-10)" 或 "(>= 0)" 的字符串，附加到字段显示名后。
    """
    if field_info is None or not hasattr(field_info, "metadata"):
        return ""

    ge_val = None
    le_val = None
    for m in field_info.metadata:
        if hasattr(m, "ge"):
            ge_val = m.ge
        if hasattr(m, "le"):
            le_val = m.le

    if ge_val is not None and le_val is not None:
        return f" ({ge_val}-{le_val})"
    if ge_val is not None:
        return f" (>= {ge_val})"
    if le_val is not None:
        return f" (<= {le_val})"
    return ""


# --- Rich UI Components ---


def _show_config_panel(display_name: str, model: BaseModel, fields: list) -> None:
    """以 rich 表格形式展示当前配置。"""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    for fname, field_info in fields:
        value = getattr(model, fname, None)
        display = _get_field_display_name(fname, field_info)
        formatted = _format_value(value, rich=True, field_name=fname)
        table.add_row(display, formatted)

    console.print(Panel(table, title=f"[bold]{display_name}[/bold]", border_style="blue"))


def _show_main_menu_header() -> None:
    """展示主菜单头部。"""
    from hczkbot import __logo__, __version__

    console.print()
    # 使用 Align.CENTER 居中单行文本
    from rich.align import Align

    console.print(
        Align.center(f"{__logo__} [bold cyan]hczkbot[{__version__}][/bold cyan]")
    )
    console.print()


def _show_section_header(title: str, subtitle: str = "") -> None:
    """展示分区头部。"""
    console.print()
    if subtitle:
        console.print(
            Panel(f"[dim]{subtitle}[/dim]", title=f"[bold]{title}[/bold]", border_style="blue")
        )
    else:
        console.print(Panel("", title=f"[bold]{title}[/bold]", border_style="blue"))


# --- Input Handlers ---


def _input_bool(display_name: str, current: bool | None) -> bool | None:
    """通过确认对话框获取布尔值输入。"""
    return _get_questionary().confirm(
        display_name,
        default=bool(current) if current is not None else False,
    ).ask()


def _input_text(display_name: str, current: Any, field_type: str, field_info=None) -> Any:
    """获取文本输入并按字段类型解析。"""
    default = _format_value_for_input(current, field_type)

    value = _get_questionary().text(f"{display_name}:", default=default).ask()

    if value is None:
        return None

    if field_type == "int":
        try:
            parsed = int(value)
        except ValueError:
            console.print("[yellow]! 数字格式无效，值未保存[/yellow]")
            return None
        if field_info:
            error = _validate_field_constraint(parsed, field_info)
            if error:
                console.print(f"[yellow]! {error}，值未保存[/yellow]")
                return None
        return parsed
    elif field_type == "float":
        try:
            parsed = float(value)
        except ValueError:
            console.print("[yellow]! 数字格式无效，值未保存[/yellow]")
            return None
        if field_info:
            error = _validate_field_constraint(parsed, field_info)
            if error:
                console.print(f"[yellow]! {error}，值未保存[/yellow]")
                return None
        return parsed
    elif field_type == "list":
        return [v.strip() for v in value.split(",") if v.strip()]
    elif field_type == "dict":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            console.print("[yellow]! JSON 格式无效，值未保存[/yellow]")
            return None

    return value


def _input_with_existing(
    display_name: str, current: Any, field_type: str, field_info=None
) -> Any:
    """处理已有值的输入：提供「保留当前值」或「输入新值」选项。"""
    has_existing = current is not None and current != "" and current != {} and current != []

    if has_existing and not isinstance(current, list):
        choice = _get_questionary().select(
            display_name,
            choices=["输入新值", "保留当前值"],
            default="保留当前值",
        ).ask()
        if choice == "保留当前值" or choice is None:
            return None

    return _input_text(display_name, current, field_type, field_info=field_info)


# --- Pydantic Model Configuration ---


def _get_current_provider(model: BaseModel) -> str:
    """从模型对象中读取当前 provider 设置（如果存在）。"""
    if hasattr(model, "provider"):
        return getattr(model, "provider", "auto") or "auto"
    return "auto"


def _input_model_with_autocomplete(
    display_name: str, current: Any, provider: str
) -> str | None:
    """获取模型输入，并提供基于已输入文本的自动补全建议。"""
    from prompt_toolkit.completion import Completer, Completion

    default = str(current) if current else ""

    class DynamicModelCompleter(Completer):
        """动态获取模型建议的补全器。"""

        def __init__(self, provider_name: str):
            self.provider = provider_name

        def get_completions(self, document, _complete_event):
            text = document.text_before_cursor
            suggestions = get_model_suggestions(text, provider=self.provider, limit=50)
            for model in suggestions:
                # 跳过不包含已输入文本的模型
                if text.lower() not in model.lower():
                    continue
                yield Completion(
                    model,
                    start_position=-len(text),
                    display=model,
                )

    value = _get_questionary().autocomplete(
        f"{display_name}:",
        choices=[""],  # 占位，实际补全由 completer 提供
        completer=DynamicModelCompleter(provider),
        default=default,
        qmark=">",
    ).ask()

    return value if value is not None else None


def _input_context_window_with_recommendation(
    display_name: str, current: Any, model_obj: BaseModel
) -> int | None:
    """获取上下文窗口输入，并支持查询推荐值。"""
    current_val = current if current else ""

    choices = ["输入新值"]
    if current_val:
        choices.append("保留当前值")
    choices.append("[?] 获取推荐值")

    choice = _get_questionary().select(
        display_name,
        choices=choices,
        default="输入新值",
    ).ask()

    if choice is None:
        return None

    if choice == "保留当前值":
        return None

    if choice == "[?] 获取推荐值":
        # 从模型对象读取模型名称
        model_name = getattr(model_obj, "model", None)
        if not model_name:
            console.print("[yellow]! 请先配置 model 字段[/yellow]")
            return None

        provider = _get_current_provider(model_obj)
        context_limit = get_model_context_limit(model_name, provider)

        if context_limit:
            console.print(f"[green]+ 推荐上下文窗口: {format_token_count(context_limit)} tokens[/green]")
            return context_limit
        else:
            console.print("[yellow]! 无法获取模型信息，请手动输入[/yellow]")
            # 继续走手动输入分支

    # 手动输入
    value = _get_questionary().text(
        f"{display_name}:",
        default=str(current_val) if current_val else "",
    ).ask()

    if value is None or value == "":
        return None

    try:
        return int(value)
    except ValueError:
        console.print("[yellow]! 数字格式无效，值未保存[/yellow]")
        return None


def _handle_model_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """处理 'model' 字段：自动补全 + 自动填充上下文窗口。"""
    provider = _get_current_provider(working_model)
    new_value = _input_model_with_autocomplete(field_display, current_value, provider)
    if new_value is not None and new_value != current_value:
        setattr(working_model, field_name, new_value)
        _try_auto_fill_context_window(working_model, new_value)


def _handle_context_window_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """处理 context_window_tokens 字段：支持查询推荐值。"""
    new_value = _input_context_window_with_recommendation(
        field_display, current_value, working_model
    )
    if new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_model_preset_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """处理 'model_preset' 字段：从已存在的预设列表中选择。"""
    preset_names = sorted(_MODEL_PRESET_CACHE)
    choices = ["(清除/不设置)"] + preset_names
    default_choice = str(current_value) if current_value else "(清除/不设置)"
    new_value = _select_with_back(field_display, choices, default=default_choice)
    if new_value is _BACK_PRESSED:
        return
    if new_value == "(清除/不设置)":
        setattr(working_model, field_name, None)
    elif new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_provider_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """处理 'provider' 字段：从已注册的 provider 列表中选择。"""
    provider_names = sorted(_get_provider_names().keys())
    choices = ["auto"] + provider_names
    default_choice = str(current_value) if current_value else "auto"
    new_value = _select_with_back(field_display, choices, default=default_choice)
    if new_value is _BACK_PRESSED:
        return
    if new_value is not None:
        setattr(working_model, field_name, new_value)


def _handle_fallback_models_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """处理 'fallback_models' 字段：基于预设列表管理备选模型。"""
    from hczkbot.config.schema import InlineFallbackConfig

    items: list[Any] = list(current_value) if isinstance(current_value, list) else []
    preset_names = sorted(_MODEL_PRESET_CACHE)

    while True:
        console.clear()
        console.print(f"[bold]{field_display}[/bold]")
        if items:
            for idx, item in enumerate(items, 1):
                if isinstance(item, InlineFallbackConfig):
                    console.print(f"  {idx}. {item.model} ({item.provider}) [内联]")
                else:
                    console.print(f"  {idx}. {item}")
        else:
            console.print("  [dim](空)[/dim]")
        console.print()

        choices = ["[+] 添加预设"]
        if items:
            choices.append("[-] 移除最后一个")
            choices.append("[X] 清空全部")
        choices.append("[完成]")
        choices.append("<- 返回")

        answer = _get_questionary().select(
            "管理备选模型:",
            choices=choices,
            qmark=">",
        ).ask()

        if answer is None or answer == "<- 返回":
            return
        if answer == "[完成]":
            setattr(working_model, field_name, items)
            return
        if answer == "[+] 添加预设":
            if not preset_names:
                console.print("[yellow]! 暂无已定义的预设[/yellow]")
                _get_questionary().press_any_key_to_continue().ask()
                continue
            add_choices = [p for p in preset_names if p not in items]
            if not add_choices:
                console.print("[yellow]! 所有预设均已添加[/yellow]")
                _get_questionary().press_any_key_to_continue().ask()
                continue
            picked = _select_with_back("选择预设:", add_choices)
            if picked is _BACK_PRESSED or picked is None:
                continue
            items.append(picked)
        elif answer == "[-] 移除最后一个" and items:
            items.pop()
        elif answer == "[X] 清空全部" and items:
            items.clear()


_FIELD_HANDLERS: dict[str, Any] = {
    "model": _handle_model_field,
    "context_window_tokens": _handle_context_window_field,
    "model_preset": _handle_model_preset_field,
    "provider": _handle_provider_field,
    "fallback_models": _handle_fallback_models_field,
}


def _is_str_or_none(annotation: Any) -> bool:
    """判断字段注解是否为 ``str | None``（或 ``Optional[str]``）。"""
    origin = get_origin(annotation)
    if origin is None:
        return False
    args = get_args(annotation)
    return str in args and type(None) in args


def _configure_pydantic_model(
    model: BaseModel,
    display_name: str,
    *,
    skip_fields: set[str] | None = None,
) -> BaseModel | None:
    """交互式配置 Pydantic 模型。

    仅当用户显式选择「完成」时返回更新后的模型；
    「返回」或取消动作会丢弃当前分区草稿。
    """
    skip_fields = skip_fields or set()
    working_model = model.model_copy(deep=True)

    fields = [
        (name, info)
        for name, info in type(working_model).model_fields.items()
        if name not in skip_fields
    ]
    if not fields:
        console.print(f"[dim]{display_name}: 无可配置字段[/dim]")
        return working_model

    def get_choices() -> list[str]:
        items = []
        for fname, finfo in fields:
            value = getattr(working_model, fname, None)
            display = _get_field_display_name(fname, finfo)
            formatted = _format_value(value, rich=False, field_name=fname)
            items.append(f"{display}: {formatted}")
        return items + ["[完成]"]

    last_field_name: str | None = None
    while True:
        console.clear()
        _show_config_panel(display_name, working_model, fields)
        choices = get_choices()
        default_choice = None
        if last_field_name:
            for idx, (fname, _) in enumerate(fields):
                if fname == last_field_name:
                    default_choice = choices[idx]
                    break
        answer = _select_with_back(
            "选择要配置的字段:", choices, default=default_choice
        )

        if answer is _BACK_PRESSED or answer is None:
            return None
        if answer == "[完成]":
            return working_model

        field_idx = next((i for i, c in enumerate(choices) if c == answer), -1)
        if field_idx < 0 or field_idx >= len(fields):
            return None

        last_field_name = fields[field_idx][0]

        field_name, field_info = fields[field_idx]
        current_value = getattr(working_model, field_name, None)
        ftype = _get_field_type_info(field_info)
        field_display = _get_field_display_name(field_name, field_info) + _get_constraint_hint(field_info)

        # 嵌套 Pydantic 模型 — 递归处理
        if ftype.type_name == "model":
            nested = current_value
            created = nested is None
            if nested is None and ftype.inner_type:
                nested = ftype.inner_type()
            if nested and isinstance(nested, BaseModel):
                updated = _configure_pydantic_model(nested, field_display)
                if updated is not None:
                    setattr(working_model, field_name, updated)
                elif created:
                    setattr(working_model, field_name, None)
            continue

        # 已注册的专用字段处理器
        handler = _FIELD_HANDLERS.get(field_name)
        if handler:
            handler(working_model, field_name, field_display, current_value)
            continue

        # 带提示的 select 字段（如 reasoning_effort）
        if field_name in _SELECT_FIELD_HINTS:
            choices_list, hint = _SELECT_FIELD_HINTS[field_name]
            select_choices = choices_list + ["(清除/不设置)"]
            console.print(f"[dim]  提示: {hint}[/dim]")
            new_value = _select_with_back(
                field_display, select_choices, default=current_value or select_choices[0]
            )
            if new_value is _BACK_PRESSED:
                continue
            if new_value == "(清除/不设置)":
                setattr(working_model, field_name, None)
            elif new_value is not None:
                setattr(working_model, field_name, new_value)
            continue

        # 通用字段输入
        if ftype.type_name == "literal" and ftype.inner_type:
            select_choices = [str(v) for v in ftype.inner_type]
            default_choice = str(current_value) if current_value in ftype.inner_type else select_choices[0]
            new_value = _select_with_back(field_display, select_choices, default=default_choice)
            if new_value is _BACK_PRESSED:
                continue
            if new_value is not None:
                setattr(working_model, field_name, new_value)
            continue
        if ftype.type_name == "bool":
            new_value = _input_bool(field_display, current_value)
        else:
            new_value = _input_with_existing(field_display, current_value, ftype.type_name, field_info=field_info)
        if new_value is not None:
            # 对于可选字符串字段，将空字符串规范化为 None，以便清空 api_key / api_base 时真正移除值
            if new_value == "" and _is_str_or_none(field_info.annotation):
                new_value = None
            setattr(working_model, field_name, new_value)


def _try_auto_fill_context_window(model: BaseModel, new_model_name: str) -> None:
    """当 context_window_tokens 仍为默认值时，尝试自动填充推荐值。

    注意:
        本函数会从 hczkbot.config.schema 导入 AgentDefaults，
        以读取 context_window_tokens 的默认值。若 schema 变化需同步更新此耦合。
    """
    # 检查 context_window_tokens 字段是否存在
    if not hasattr(model, "context_window_tokens"):
        return

    current_context = getattr(model, "context_window_tokens", None)

    # 仅在当前值仍为默认（65536）时自动填充；用户已修改的不覆盖
    from hczkbot.config.schema import AgentDefaults

    default_context = AgentDefaults.model_fields["context_window_tokens"].default

    if current_context != default_context:
        return  # 用户已自定义，不覆盖

    provider = _get_current_provider(model)
    context_limit = get_model_context_limit(new_model_name, provider)

    if context_limit:
        setattr(model, "context_window_tokens", context_limit)
        console.print(f"[green]+ 自动填充上下文窗口: {format_token_count(context_limit)} tokens[/green]")
    else:
        console.print("[dim](i) 无法自动填充上下文窗口（模型未在数据库中）[/dim]")


# --- Model Preset Configuration ---


def _sync_preset_cache(config: Config) -> None:
    """根据 config 同步模块级预设名称缓存。"""
    _MODEL_PRESET_CACHE.clear()
    _MODEL_PRESET_CACHE.update(config.model_presets.keys())


def _configure_model_presets(config: Config) -> None:
    """配置模型预设（增删改查）。"""
    _sync_preset_cache(config)

    def get_preset_choices() -> list[str]:
        choices: list[str] = []
        for name, preset in config.model_presets.items():
            choices.append(f"{name} ({preset.model})")
        choices.append("[+] 添加新预设")
        choices.append("<- 返回")
        return choices

    last_preset_name: str | None = None
    while True:
        try:
            console.clear()
            _show_section_header(
                "模型预设",
                "创建、编辑或删除命名预设，便于快速切换模型参数",
            )
            choices = get_preset_choices()
            default_choice = None
            if last_preset_name:
                for c in choices:
                    if c.startswith(last_preset_name + " ("):
                        default_choice = c
                        break
            answer = _select_with_back(
                "选择预设:", choices, default=default_choice
            )

            if answer is _BACK_PRESSED or answer is None or answer == "<- 返回":
                break

            assert isinstance(answer, str)

            if answer == "[+] 添加新预设":
                name_input = _get_questionary().text(
                    "预设名称:",
                    validate=lambda t: True if t and t.strip() else "名称不能为空",
                ).ask()
                if not name_input:
                    continue
                name = name_input.strip()
                if name in config.model_presets:
                    console.print(f"[yellow]! 预设 '{name}' 已存在[/yellow]")
                    _pause()
                    continue
                if name == "default":
                    console.print("[yellow]! 'default' 是保留名称（由 Agent Settings 自动生成）[/yellow]")
                    _pause()
                    continue
                new_preset = ModelPresetConfig(model="")
                updated = _configure_pydantic_model(new_preset, f"新预设: {name}")
                if updated is not None:
                    config.model_presets[name] = updated
                    _sync_preset_cache(config)
                    last_preset_name = name
                continue

            # 编辑或删除已有预设
            preset_name = answer.split(" (", 1)[0]
            preset = config.model_presets.get(preset_name)
            if preset is None:
                continue

            last_preset_name = preset_name

            choices = ["编辑", "取消"]
            if preset_name != "default":
                choices.insert(1, "删除")
            action = _select_with_back(
                f"预设: {preset_name}",
                choices,
                default="编辑",
            )
            if action is _BACK_PRESSED or action == "取消" or action is None:
                continue

            if action == "删除":
                confirm = _get_questionary().confirm(
                    f"确认删除预设 '{preset_name}'?",
                    default=False,
                ).ask()
                if confirm:
                    del config.model_presets[preset_name]
                    _sync_preset_cache(config)
                    last_preset_name = None
                continue

            if action == "编辑":
                updated = _configure_pydantic_model(preset, f"编辑预设: {preset_name}")
                if updated is not None:
                    config.model_presets[preset_name] = updated
                    _sync_preset_cache(config)

        except KeyboardInterrupt:
            console.print("\n[dim]返回主菜单...[/dim]")
            break


# --- Provider Configuration ---


@lru_cache(maxsize=1)
def _get_provider_info() -> dict[str, tuple[str, bool, bool, str]]:
    """从 registry 获取 provider 信息（带缓存）。"""
    from hczkbot.providers.registry import PROVIDERS

    return {
        spec.name: (
            spec.display_name or spec.name,
            spec.is_gateway,
            spec.is_local,
            spec.default_api_base,
        )
        for spec in PROVIDERS
        if not spec.is_oauth
    }


def _get_provider_names() -> dict[str, str]:
    """获取 provider 显示名称映射。"""
    info = _get_provider_info()
    return {name: data[0] for name, data in info.items() if name}


def _configure_provider(config: Config, provider_name: str) -> None:
    """配置单个 LLM provider。"""
    provider_config = getattr(config.providers, provider_name, None)
    if provider_config is None:
        console.print(f"[red]未知 provider: {provider_name}[/red]")
        return

    display_name = _get_provider_names().get(provider_name, provider_name)
    info = _get_provider_info()
    default_api_base = info.get(provider_name, (None, None, None, None))[3]

    if default_api_base and not provider_config.api_base:
        provider_config.api_base = default_api_base

    updated_provider = _configure_pydantic_model(
        provider_config,
        display_name,
    )
    if updated_provider is not None:
        setattr(config.providers, provider_name, updated_provider)


def _configure_providers(config: Config) -> None:
    """配置 LLM provider。"""

    def get_provider_choices() -> list[str]:
        """构建带配置状态标记的 provider 选项列表。"""
        choices = []
        for name, display in _get_provider_names().items():
            provider = getattr(config.providers, name, None)
            if provider and provider.api_key:
                choices.append(f"{display} *")
            else:
                choices.append(display)
        return choices + ["<- 返回"]

    last_provider_key: str | None = None
    while True:
        try:
            console.clear()
            _show_section_header("LLM Providers", "选择要配置 API key 与端点的 provider")
            choices = get_provider_choices()
            default_choice = None
            if last_provider_key:
                display = _get_provider_names().get(last_provider_key)
                if display:
                    for c in choices:
                        if c.replace(" *", "") == display:
                            default_choice = c
                            break
            answer = _select_with_back(
                "选择 provider:", choices, default=default_choice
            )

            if answer is _BACK_PRESSED or answer is None or answer == "<- 返回":
                break

            # 类型守卫：此时 answer 已确定为字符串
            assert isinstance(answer, str)
            # 从选项文本中提取 provider 名称（去除 " *" 后缀）
            provider_name = answer.replace(" *", "")
            # 通过显示名反查 provider key
            for name, display in _get_provider_names().items():
                if display == provider_name:
                    last_provider_key = name
                    _configure_provider(config, name)
                    break

        except KeyboardInterrupt:
            console.print("\n[dim]返回主菜单...[/dim]")
            break


# --- Channel Configuration ---


@lru_cache(maxsize=1)
def _get_channel_info() -> dict[str, tuple[str, type[BaseModel]]]:
    """从 channel 模块获取信息（显示名 + 配置类）。"""
    import importlib

    from hczkbot.channels.registry import discover_all

    result: dict[str, tuple[str, type[BaseModel]]] = {}
    for name, channel_cls in discover_all().items():
        try:
            mod = importlib.import_module(f"hczkbot.channels.{name}")
            config_name = channel_cls.__name__.replace("Channel", "Config")
            config_cls = getattr(mod, config_name, None)
            if config_cls and isinstance(config_cls, type) and issubclass(config_cls, BaseModel):
                display_name = getattr(channel_cls, "display_name", name.capitalize())
                result[name] = (display_name, config_cls)
        except Exception:
            logger.warning("加载 channel 模块失败: {}", name)
    return result


def _get_channel_names() -> dict[str, str]:
    """获取 channel 显示名称映射。"""
    return {name: info[0] for name, info in _get_channel_info().items()}


def _get_channel_config_class(channel: str) -> type[BaseModel] | None:
    """获取 channel 的配置类。"""
    entry = _get_channel_info().get(channel)
    return entry[1] if entry else None


def _configure_channel(config: Config, channel_name: str) -> None:
    """配置单个 channel。"""
    channel_dict = getattr(config.channels, channel_name, None)
    if channel_dict is None:
        channel_dict = {}
        setattr(config.channels, channel_name, channel_dict)

    display_name = _get_channel_names().get(channel_name, channel_name)
    config_cls = _get_channel_config_class(channel_name)

    if config_cls is None:
        console.print(f"[red]未找到 {display_name} 的配置类[/red]")
        return

    model = config_cls.model_validate(channel_dict) if channel_dict else config_cls()

    updated_channel = _configure_pydantic_model(
        model,
        display_name,
    )
    if updated_channel is not None:
        new_dict = updated_channel.model_dump(by_alias=True, exclude_none=True)
        setattr(config.channels, channel_name, new_dict)


def _configure_channels(config: Config) -> None:
    """配置聊天 channel。"""
    channel_names = list(_get_channel_names().keys())
    choices = channel_names + ["<- 返回"]

    last_choice: str | None = None
    while True:
        try:
            console.clear()
            _show_section_header("聊天 Channel", "选择要配置连接参数的 channel")
            answer = _select_with_back(
                "选择 channel:", choices, default=last_choice
            )

            if answer is _BACK_PRESSED or answer is None or answer == "<- 返回":
                break

            # 类型守卫：此时 answer 已确定为字符串
            assert isinstance(answer, str)
            last_choice = answer
            _configure_channel(config, answer)
        except KeyboardInterrupt:
            console.print("\n[dim]返回主菜单...[/dim]")
            break


# --- MCP Servers ---


def _configure_mcp_servers(config: Config) -> None:
    """配置 MCP 服务器（预设目录 + 手动添加）。"""
    from hczkbot.webui.mcp_presets_api import MCP_PRESETS

    while True:
        console.clear()
        _show_section_header("MCP 服务器", "管理 MCP 服务器配置 — 为智能体扩展工具能力")

        existing = config.tools.mcp_servers or {}
        if existing:
            console.print("[bold]已配置的服务器:[/bold]\n")
            for name, srv in existing.items():
                srv_type = getattr(srv, "type", None) or "auto"
                if srv_type == "stdio":
                    detail = f"{srv.command} {' '.join(srv.args)}"
                elif srv_type in ("sse", "streamableHttp"):
                    detail = srv.url
                else:
                    detail = str(srv.command or srv.url or "")
                console.print(f"  • [cyan]{name}[/cyan] ({srv_type}): {detail}")
            console.print()
        else:
            console.print("[dim]暂未配置 MCP 服务器[/dim]\n")

        choices = ["[+] 从预设目录添加", "[+] 手动添加"]
        if existing:
            choices.append("[-] 删除服务器")
        choices += ["[Done] 完成", "<- Back"]

        answer = _get_questionary().select("选择操作:", choices=choices, qmark=">").ask()
        if answer is None or answer in ("<- Back", "[Done]"):
            return
        if answer == "[+] 从预设目录添加":
            _add_mcp_from_preset(config, MCP_PRESETS)
        elif answer == "[+] 手动添加":
            _add_mcp_custom(config)
        elif answer == "[-] 删除服务器":
            _remove_mcp_server(config)


def _add_mcp_from_preset(config: Config, presets: tuple) -> None:
    """从预设目录添加 MCP 服务器。"""
    existing_names = set((config.tools.mcp_servers or {}).keys())
    available = [p for p in presets if p.name not in existing_names]

    if not available:
        console.print("[yellow]所有预设已添加，或暂无可用预设[/yellow]")
        _get_questionary().press_any_key_to_continue().ask()
        return

    choices = [f"{p.display_name} — {p.description[:60]}" for p in available]
    choices.append("<- Back")

    answer = _get_questionary().select("选择预设:", choices=choices, qmark=">").ask()
    if answer is None or answer == "<- Back":
        return

    idx = choices.index(answer)
    if idx >= len(available):
        return
    preset = available[idx]

    console.print(f"\n[bold]{preset.display_name}[/bold]")
    console.print(f"  {preset.description}")
    console.print(f"  文档: {preset.docs_url}")
    console.print(f"  要求: {preset.requires}\n")

    server = preset.server.model_copy(deep=True) if preset.server else None
    if server is None:
        console.print("[red]! 该预设没有服务器配置模板[/red]")
        _get_questionary().press_any_key_to_continue().ask()
        return

    env_extra: dict[str, str] = {}
    args_extra: list[str] = list(server.args)
    url_mod = server.url
    header_extra: dict[str, str] = dict(server.headers)

    for field in preset.fields:
        label = field.label
        opt = "" if field.required else "（可选，回车跳过）"
        hint = f" [dim]例: {field.placeholder}[/dim]" if field.placeholder else ""
        value = _get_questionary().text(f"请输入 {label}{opt}:{hint}", default="").ask()
        if value is None:
            return
        if not value and field.required:
            console.print(f"[red]! {label} 是必填项[/red]")
            _get_questionary().press_any_key_to_continue().ask()
            return
        if not value:
            continue

        t_type, t_key = field.target
        if t_type == "env":
            env_extra[t_key] = value
        elif t_type == "arg":
            args_extra.extend([t_key, value])
        elif t_type == "url_param":
            sep = "&" if "?" in (url_mod or "") else "?"
            url_mod = f"{url_mod}{sep}{t_key}={value}"
        elif t_type == "header":
            header_extra[t_key] = value

    if env_extra:
        server.env = {**server.env, **env_extra}
    if args_extra != list(preset.server.args):
        server.args = args_extra
    if url_mod != preset.server.url:
        server.url = url_mod
    if header_extra != preset.server.headers:
        server.headers = header_extra

    if config.tools.mcp_servers is None:
        config.tools.mcp_servers = {}
    config.tools.mcp_servers[preset.name] = server
    console.print(f"\n[green]✓ 已添加 {preset.name} MCP 服务器[/green]")
    _get_questionary().press_any_key_to_continue().ask()


def _add_mcp_custom(config: Config) -> None:
    """手动添加自定义 MCP 服务器。"""
    from hczkbot.config.schema import MCPServerConfig

    name = _get_questionary().text("服务器名称（小写字母、数字、横线）:", default="").ask()
    if not name:
        return
    name = name.strip().lower()
    existing = config.tools.mcp_servers or {}
    if name in existing:
        console.print(f"[yellow]! 名为 '{name}' 的服务器已存在[/yellow]")
        _get_questionary().press_any_key_to_continue().ask()
        return

    transport = _get_questionary().select(
        "传输类型:", choices=["stdio", "streamableHttp", "sse"], default="stdio", qmark=">"
    ).ask()
    if transport is None:
        return

    server = MCPServerConfig(type=transport)
    if transport == "stdio":
        command = _get_questionary().text("命令 (如 npx):", default="npx").ask()
        if command:
            server.command = command
        args_str = _get_questionary().text("参数 (逗号分隔):", default="").ask()
        if args_str:
            server.args = [a.strip() for a in args_str.split(",") if a.strip()]
    else:
        url = _get_questionary().text("URL:", default="").ask()
        if url:
            server.url = url

    timeout_str = _get_questionary().text("工具超时秒数 (默认 30):", default="30").ask()
    if timeout_str:
        try:
            server.tool_timeout = int(timeout_str)
        except ValueError:
            pass

    if config.tools.mcp_servers is None:
        config.tools.mcp_servers = {}
    config.tools.mcp_servers[name] = server
    console.print(f"\n[green]✓ 已添加 {name} MCP 服务器[/green]")
    _get_questionary().press_any_key_to_continue().ask()


def _remove_mcp_server(config: Config) -> None:
    """删除 MCP 服务器。"""
    existing = config.tools.mcp_servers or {}
    if not existing:
        return
    names = list(existing.keys())
    answer = _get_questionary().select("选择要删除的服务器:", choices=names + ["<- Back"], qmark=">").ask()
    if answer is None or answer == "<- Back":
        return
    confirm = _get_questionary().confirm(f"确认删除 {answer}?", default=False).ask()
    if confirm:
        del existing[answer]
        console.print(f"[green]✓ 已删除 {answer}[/green]")
        _get_questionary().press_any_key_to_continue().ask()


# --- General Settings ---

_SETTINGS_SECTIONS: dict[str, tuple[str, str, set[str] | None]] = {
    "Agent Settings": ("Agent 默认设置", "配置默认模型、温度及行为参数", None),
    "Channel Common": ("Channel 通用设置", "配置跨 channel 行为：进度推送、工具提示、重试次数", None),
    "API Server": ("API 服务器", "配置 OpenAI 兼容 API 端点", None),
    "Gateway": ("Gateway 设置", "配置服务监听 host 与 port", None),
    "Tools": ("Tools 工具设置", "配置 Web 搜索、Shell 执行等工具", {"mcp_servers"}),
    "Transcription": ("语音转录", "配置语音转文字（provider/model/language/时长限制）", None),
}

_SETTINGS_GETTER = {
    "Agent Settings": lambda c: c.agents.defaults,
    "Channel Common": lambda c: c.channels,
    "API Server": lambda c: c.api,
    "Gateway": lambda c: c.gateway,
    "Tools": lambda c: c.tools,
    "Transcription": lambda c: c.transcription,
}

_SETTINGS_SETTER = {
    "Agent Settings": lambda c, v: setattr(c.agents, "defaults", v),
    "Channel Common": lambda c, v: setattr(c, "channels", v),
    "API Server": lambda c, v: setattr(c, "api", v),
    "Gateway": lambda c, v: setattr(c, "gateway", v),
    "Tools": lambda c, v: setattr(c, "tools", v),
    "Transcription": lambda c, v: setattr(c, "transcription", v),
}


def _configure_general_settings(config: Config, section: str) -> None:
    """配置通用设置分区（头部展示 + 模型编辑 + 写回）。"""
    meta = _SETTINGS_SECTIONS.get(section)
    if not meta:
        return
    display_name, subtitle, skip = meta
    model = _SETTINGS_GETTER[section](config)
    updated = _configure_pydantic_model(model, display_name, skip_fields=skip)
    if updated is not None:
        _SETTINGS_SETTER[section](config, updated)


# --- Summary ---


def _summarize_model(obj: BaseModel) -> list[tuple[str, str]]:
    """递归汇总 Pydantic 模型，返回 (字段, 值) 元组列表。"""
    items: list[tuple[str, str]] = []
    for field_name, field_info in type(obj).model_fields.items():
        value = getattr(obj, field_name, None)
        if value is None or value == "" or value == {} or value == []:
            continue
        display = _get_field_display_name(field_name, field_info)
        ftype = _get_field_type_info(field_info)
        if ftype.type_name == "model" and isinstance(value, BaseModel):
            for nested_field, nested_value in _summarize_model(value):
                items.append((f"{display}.{nested_field}", nested_value))
            continue
        formatted = _format_value(value, rich=False, field_name=field_name)
        if formatted != "[not set]":
            items.append((display, formatted))
    return items


def _print_summary_panel(rows: list[tuple[str, str]], title: str) -> None:
    """构建双列汇总面板并打印。"""
    if not rows:
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    for field, value in rows:
        table.add_row(field, value)
    console.print(Panel(table, title=f"[bold]{title}[/bold]", border_style="blue"))


def _show_summary(config: Config) -> None:
    """使用 rich 展示配置汇总。"""
    console.print()

    # Providers
    provider_rows = []
    for name, display in _get_provider_names().items():
        # 状态：已配置 / 未配置
        provider = getattr(config.providers, name, None)
        status = "[green]已配置[/green]" if (provider and provider.api_key) else "[dim]未配置[/dim]"
        provider_rows.append((display, status))
    _print_summary_panel(provider_rows, "LLM Providers")

    # Channels（聊天渠道）
    channel_rows = []
    for name, display in _get_channel_names().items():
        channel = getattr(config.channels, name, None)
        if channel:
            enabled = (
                channel.get("enabled", False)
                if isinstance(channel, dict)
                else getattr(channel, "enabled", False)
            )
            status = "[green]已启用[/green]" if enabled else "[dim]已禁用[/dim]"
        else:
            status = "[dim]未配置[/dim]"
        channel_rows.append((display, status))
    _print_summary_panel(channel_rows, "聊天 Channel")

    # 模型预设
    preset_rows = []
    for name, preset in config.model_presets.items():
        preset_rows.append((name, f"{preset.model} (ctx={preset.context_window_tokens})"))
    _print_summary_panel(preset_rows, "模型预设")

    # 各设置分区汇总
    for title, model in [
        ("Agent Settings", config.agents.defaults),
        ("Channel Common", config.channels),
        ("API Server", config.api),
        ("Gateway", config.gateway),
        ("Tools", config.tools),
    ]:
        _print_summary_panel(_summarize_model(model), title)

    _pause()


def _pause() -> None:
    """清屏前暂停等待用户确认。"""
    _get_questionary().text("按回车键继续...", default="").ask()


# --- Main Entry Point ---


def _has_unsaved_changes(original: Config, current: Config) -> bool:
    """当本次引导会话已产生变更时返回 True。"""
    return original.model_dump(by_alias=True) != current.model_dump(by_alias=True)


def _prompt_main_menu_exit(has_unsaved_changes: bool) -> str:
    """决定如何离开主菜单。"""
    if not has_unsaved_changes:
        return "discard"

    answer = _get_questionary().select(
        "当前有未保存的更改，请选择操作:",
        choices=[
            "[S] 保存并退出",
            "[X] 不保存退出",
            "[R] 继续编辑",
        ],
        default="[R] 继续编辑",
        qmark=">",
    ).ask()

    if answer == "[S] 保存并退出":
        return "save"
    if answer == "[X] 不保存退出":
        return "discard"
    return "resume"


def run_onboard(initial_config: Config | None = None) -> OnboardResult:
    """运行交互式配置引导问卷。

    Args:
        initial_config: 可选的预加载配置作为起点。
                       若为 None，则从配置文件加载或使用默认值新建。
    """
    _get_questionary()

    if initial_config is not None:
        base_config = initial_config.model_copy(deep=True)
    else:
        config_path = get_config_path()
        if config_path.exists():
            base_config = load_config()
        else:
            base_config = Config()

    original_config = base_config.model_copy(deep=True)
    config = base_config.model_copy(deep=True)
    _sync_preset_cache(config)

    last_main_choice: str | None = None
    while True:
        console.clear()
        _show_main_menu_header()

        try:
            answer = _get_questionary().select(
                "请选择要配置的项目:",
                choices=[
                    "[P] LLM Provider",
                    "[M] 模型预设",
                    "[C] 聊天 Channel",
                    "[H] Channel 通用设置",
                    "[A] Agent 设置",
                    "[I] API 服务器",
                    "[G] Gateway",
                    "[T] Tools 工具",
                    "[N] MCP 服务器",
                    "[R] 语音转录",
                    "[V] 查看配置汇总",
                    "[S] 保存并退出",
                    "[X] 不保存退出",
                ],
                default=last_main_choice,
                qmark=">",
            ).ask()
        except KeyboardInterrupt:
            answer = None

        if answer is None:
            action = _prompt_main_menu_exit(_has_unsaved_changes(original_config, config))
            if action == "save":
                return OnboardResult(config=config, should_save=True)
            if action == "discard":
                return OnboardResult(config=original_config, should_save=False)
            continue

        _menu_dispatch = {
            "[P] LLM Provider": lambda: _configure_providers(config),
            "[M] 模型预设": lambda: _configure_model_presets(config),
            "[C] 聊天 Channel": lambda: _configure_channels(config),
            "[H] Channel 通用设置": lambda: _configure_general_settings(config, "Channel Common"),
            "[A] Agent 设置": lambda: _configure_general_settings(config, "Agent Settings"),
            "[I] API 服务器": lambda: _configure_general_settings(config, "API Server"),
            "[G] Gateway": lambda: _configure_general_settings(config, "Gateway"),
            "[T] Tools 工具": lambda: _configure_general_settings(config, "Tools"),
            "[N] MCP 服务器": lambda: _configure_mcp_servers(config),
            "[R] 语音转录": lambda: _configure_general_settings(config, "Transcription"),
            "[V] 查看配置汇总": lambda: _show_summary(config),
        }

        if answer == "[S] 保存并退出":
            return OnboardResult(config=config, should_save=True)
        if answer == "[X] 不保存退出":
            return OnboardResult(config=original_config, should_save=False)

        action_fn = _menu_dispatch.get(answer)
        if action_fn:
            last_main_choice = answer
            action_fn()
