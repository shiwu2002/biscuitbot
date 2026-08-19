"""Runtime-specific helper functions and constants."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from biscuitbot.utils.helpers import stringify_text_blocks

_MAX_REPEAT_EXTERNAL_LOOKUPS = 2

# Third same-target workspace violation in a turn escalates to "stop retrying".
_MAX_REPEAT_WORKSPACE_VIOLATIONS = 2

# Same tool call (after normalization) repeating this many times in a turn is
# treated as a stuck loop and blocked. Attempts 1..N pass; attempt N+1 errors.
_MAX_REPEAT_TOOL_CALLS = 3

EMPTY_FINAL_RESPONSE_MESSAGE = (
    "I completed the tool steps but couldn't produce a final answer. "
    "Please try again or narrow the task."
)

FINALIZATION_RETRY_PROMPT = (
    "Please provide your response to the user based on the conversation above."
)

BUDGET_EXHAUSTED_FINALIZATION_PROMPT = (
    "The tool-call budget for this turn is exhausted. Based only on the "
    "conversation and tool results above, provide a concise final response to "
    "the user. Do not call or request tools. Do not claim the task is complete "
    "unless the evidence above clearly shows it is complete. State what was "
    "done, what remains, and the best next step if anything is incomplete."
)

LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)

SUSTAINED_GOAL_CONTINUE_PROMPT = (
    "You have an active sustained goal. Please continue working toward the "
    "objective using your tools, or call complete_goal if the work is truly finished."
)


def empty_tool_result_message(tool_name: str) -> str:
    """Short prompt-safe marker for tools that completed without visible output."""
    return f"({tool_name} completed with no output)"


def ensure_nonempty_tool_result(tool_name: str, content: Any) -> Any:
    """Replace semantically empty tool results with a short marker string."""
    if content is None:
        return empty_tool_result_message(tool_name)
    if isinstance(content, str) and not content.strip():
        return empty_tool_result_message(tool_name)
    if isinstance(content, list):
        if not content:
            return empty_tool_result_message(tool_name)
        text_payload = stringify_text_blocks(content)
        if text_payload is not None and not text_payload.strip():
            return empty_tool_result_message(tool_name)
    return content


def is_blank_text(content: str | None) -> bool:
    """True when *content* is missing or only whitespace."""
    return content is None or not content.strip()


def build_finalization_retry_message() -> dict[str, str]:
    """A short no-tools-allowed prompt for final answer recovery."""
    return {"role": "user", "content": FINALIZATION_RETRY_PROMPT}


def build_budget_exhausted_finalization_message() -> dict[str, str]:
    """Prompt the model for a no-tools final response after budget exhaustion."""
    return {"role": "user", "content": BUDGET_EXHAUSTED_FINALIZATION_PROMPT}


def build_length_recovery_message() -> dict[str, str]:
    """Prompt the model to continue after hitting output token limit."""
    return {"role": "user", "content": LENGTH_RECOVERY_PROMPT}


def build_goal_continue_message(custom: str | None = None) -> dict[str, str]:
    """Prompt the model to continue when a sustained goal is still active."""
    return {"role": "user", "content": custom or SUSTAINED_GOAL_CONTINUE_PROMPT}


def external_lookup_signature(tool_name: str, arguments: Any) -> str | None:
    """Stable signature for repeated external lookups we want to throttle."""
    if not isinstance(arguments, dict):
        return None
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if url:
            return f"web_fetch:{url.lower()}"
    if tool_name == "web_search":
        query = str(arguments.get("query") or arguments.get("search_term") or "").strip()
        if query:
            return f"web_search:{query.lower()}"
    return None


def repeated_external_lookup_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Block repeated external lookups after a small retry budget."""
    signature = external_lookup_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_EXTERNAL_LOOKUPS:
        return None
    logger.warning(
        "Blocking repeated external lookup {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: repeated external lookup blocked. "
        "Use the results you already have to answer, or try a meaningfully different source."
    )


# Workspace-boundary violations are soft errors, with per-target throttling.

_OUTSIDE_PATH_PATTERN = re.compile(r"(?:^|[\s|>'\"])((?:/[^\s\"'>;|<]+)|(?:~[^\s\"'>;|<]+))")


def workspace_violation_signature(
    tool_name: str,
    arguments: Any,
) -> str | None:
    """Return a stable cross-tool signature for the outside-workspace target."""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "target", "source", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_violation_target(val.strip())

    if tool_name in {"exec", "shell"}:
        cmd = str(arguments.get("command") or "").strip()
        if cmd:
            match = _OUTSIDE_PATH_PATTERN.search(cmd)
            if match:
                return _normalize_violation_target(match.group(1))
        cwd = str(arguments.get("working_dir") or "").strip()
        if cwd:
            return _normalize_violation_target(cwd)

    return None


def _normalize_violation_target(raw: str) -> str:
    """Normalize *raw* path so that equivalent spellings collide on the same key."""
    try:
        normalized = Path(raw).expanduser().resolve().as_posix()
    except Exception:
        normalized = raw.replace("\\", "/")
    return f"violation:{normalized}".lower()


def repeated_workspace_violation_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Return an escalated error after repeated bypass attempts."""
    signature = workspace_violation_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_WORKSPACE_VIOLATIONS:
        return None
    logger.warning(
        "Escalating repeated workspace bypass attempt {} (attempt {})",
        signature[:160],
        count,
    )
    target = signature.split("violation:", 1)[1] if "violation:" in signature else signature
    return (
        "Error: refusing repeated workspace-bypass attempts.\n"
        f"You have tried to access '{target}' (or an equivalent path) "
        f"{count} times in this turn. This is a hard policy boundary -- "
        "switching tools, shell tricks, working_dir overrides, symlinks, "
        "or base64 piping will NOT change the answer. Stop retrying. "
        "If the user genuinely needs this resource, tell them you cannot "
        "access it and ask how they want to proceed (e.g. copy the file "
        "into the workspace, or disable restrict_to_workspace for this run)."
    )


# Generic loop detection: block a tool call that repeats an identical
# (normalized) call too many times in a single turn. This catches the common
# "stuck model" pattern — e.g. re-running `find ... | grep ...` with a varying
# grep tail — which the external-lookup and workspace-violation throttles miss.

_SHELL_SEPARATORS = ("|", "&&", "||", ";")
_TRAILING_REDIRECT = re.compile(r"\s+\d?>>?\s*(?:/dev/null|&[12])\s*$")


def _shell_command_signature(cmd: str) -> str:
    """Normalize a shell command down to its primary invocation.

    The model often retries the same ``find``/``grep`` base command with a
    varying ``| grep ... | head`` tail or a trailing ``2>/dev/null`` redirect.
    Collapsing those onto the base command is what lets the loop detector treat
    them as the same repeated call instead of a fresh one each time.
    """
    base = cmd.strip()
    for sep in _SHELL_SEPARATORS:
        base = base.split(sep, 1)[0]
    return _TRAILING_REDIRECT.sub("", base).strip()


def tool_call_signature(tool_name: str, arguments: Any) -> str | None:
    """Return a stable signature for a tool call, or None when it can't be made.

    Used to detect repeated identical tool calls. Shell/exec commands are
    normalized via :func:`_shell_command_signature` so that a varying pipe tail
    still collides on the same key; other tools serialize their arguments.
    """
    if not isinstance(arguments, dict):
        return None
    # Web lookups already have a dedicated throttle; skip them to avoid
    # double-counting the same retry across two detectors.
    if tool_name in {"web_fetch", "web_search"}:
        return None
    if tool_name in {"exec", "shell"}:
        cmd = str(arguments.get("command") or "").strip()
        if not cmd:
            return None
        return f"{tool_name}:{_shell_command_signature(cmd)}"
    try:
        serialized = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return f"{tool_name}:{serialized}"


def repeated_tool_call_error(
    tool_name: str,
    arguments: Any,
    seen_counts: dict[str, int],
) -> str | None:
    """Return a soft error after the same (normalized) tool call repeats too often.

    Attempts up to ``_MAX_REPEAT_TOOL_CALLS`` pass through; the next identical
    call is blocked and the model is nudged to change approach.
    """
    signature = tool_call_signature(tool_name, arguments)
    if signature is None:
        return None
    count = seen_counts.get(signature, 0) + 1
    seen_counts[signature] = count
    if count <= _MAX_REPEAT_TOOL_CALLS:
        return None
    logger.warning(
        "Blocking repeated tool call {} on attempt {}",
        signature[:160],
        count,
    )
    return (
        "Error: you have repeated this same tool call several times without "
        "making progress. Stop retrying. Use the results you already have, or "
        "switch to a meaningfully different approach."
    )
