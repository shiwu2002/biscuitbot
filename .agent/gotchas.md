# Common Gotchas

## Do not use `ruff format`

`CONTRIBUTING.md` mentions `ruff format`, but **do not run it** — it destroys git blame history. Only `ruff check` should be used.

## Config `${VAR}` References

`config/loader.py` resolves `${VAR}` patterns in `config.json` at load time. This is **not** a shell-like default-value syntax. If the environment variable is missing, `load_config` raises `ValueError` and the agent falls back to default configuration.

Example valid usage:
```json
{ "providers": { "openrouter": { "apiKey": "${OPENROUTER_KEY}" } } }
```

## Windows Compatibility

hczkbot explicitly supports Windows. Key differences to keep in mind:
- `ExecTool` uses `cmd /c` on Windows instead of `sh -c` (`shell.py`).
- `cli/commands.py` forces `sys.stdout`/`stderr` to UTF-8 on startup to handle emoji and multilingual input.
- MCP stdio server commands are normalized for Windows path separators (`mcp.py`).
- Always use `pathlib.Path` for path manipulation; do not assume `/` separators.

## Prompt Templates

Agent system prompts and scenario-specific instructions live in `hczkbot/templates/` as Jinja2 markdown files (`identity.md`, `platform_policy.md`, `HEARTBEAT.md`, `SOUL.md`, etc.). Changing these files alters agent behavior as directly as changing Python code. They are loaded by `utils/prompt_templates.py`.

Tool descriptions, skills, and replayed session history also shape model behavior. Treat changes to those surfaces like runtime code: keep them narrow, add a focused regression test when possible, and avoid teaching the model to repeat internal markers, local paths, or tool-call text.

## Context Pollution Persists

Anything written into memory, session history, or prompt inputs can be replayed into future LLM calls. Metadata such as timestamps, local media paths, tool-call echoes, and raw fallback dumps must be bounded and sanitized before they become examples for the model to imitate.

## Skills as Extension Point

Built-in skills live in `hczkbot/skills/` (markdown + YAML frontmatter format). Agent capabilities that are "know-how" rather than code should be added as skills, not hardcoded into the agent loop. External skills can be published to and installed from ClawHub.

## Atomic Session Writes

`agent/memory.py` writes `history.jsonl` atomically (temp file + fsync + rename + directory fsync). This guarantees durability across crashes. Do not replace this with a plain `open(..., "w")` write.

## MCP Cancel Scopes Are Task-Local

anyio cancel scopes (used by MCP SDK's `stdio_client` async generator) are **task-local**: they must be exited in the same `asyncio.Task` that entered them. Otherwise anyio raises `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`, and the `stdio_client` generator leaks to a noisy GC-finalizer traceback.

Three paths used to trigger this:

1. **Gateway/interactive shutdown** — `cli/commands.py` runs `agent.run()` inside an `asyncio.gather`/`create_task` sub-task (cancel scope entered here), but called `close_mcp()` from the main task's `finally` (cancel scope exited here). Fixed by wrapping `_connect_mcp()` + `_run_main_loop()` in `AgentLoop.run()`'s own `try/finally` and calling `close_mcp()` there — same task. If the task is being cancelled, call `Task.uncancel()` first so the cleanup `await`s can finish.

2. **Reconnect from a `_dispatch` sub-task (closing the old stack)** — a timed-out MCP tool call triggers `_reconnect_after_timeout → _close_server → stack.aclose()`, but the stack was opened in the `run()` task. Fixed with a **deferred close**: `_close_server` checks `asyncio.current_task() is not _mcp_owner_task`; if so and the owner is alive, it moves the stack to `_mcp_deferred_stacks` and returns. The owner task drains that list via `_close_deferred_mcp_stacks()` from the main loop's `TimeoutError` branch and from `close_mcp()`.

3. **Reconnect from a `_dispatch` sub-task (opening the NEW stack)** — the deferred close above only handled *closing* the old stack. `_refresh_terminated_server` also *creates* a new stack by calling `connect_mcp_servers`, which enters a fresh `stdio_client` cancel scope **in the calling task**. When called from the reconnect closure (running in the `_dispatch` sub-task), the new stack's cancel scope got affiliated to that sub-task; when the sub-task ended, `close_mcp()` in the owner task couldn't exit it → same `RuntimeError` + leaked generator. Fixed with a **deferred reconnect**: the reconnect closure in `_attach_reconnect_handlers` checks `asyncio.current_task() is not _mcp_owner_task`; if so and the owner is alive, it enqueues `(server_name, tool_name, stale_tool, future)` onto `state._mcp_reconnect_requests` and `await`s the future. The owner task drains that queue via `_process_mcp_reconnects()` (called from the main loop's `TimeoutError` branch) and runs `_refresh_terminated_server` itself, so the new cancel scope is entered in the owner task. `close_mcp()` cancels any pending futures first so blocked sub-tasks unwind on shutdown. This works without deadlock because `_dispatch` runs as a background task (`asyncio.create_task`), leaving the owner free to hit its 1s idle branch.

**Rule of thumb**: never call `stack.aclose()` on an MCP `AsyncExitStack`, and never call `connect_mcp_servers` (which enters `stdio_client` cancel scopes), unless you are in the task that owns the MCP stacks (tracked as `AgentLoop._mcp_owner_task`). When you can't, defer it instead — close via `_mcp_deferred_stacks`, reconnect via `_mcp_reconnect_requests`. Subagents are unaffected — they use an isolated `ToolRegistry` with `scope="subagent"` only, and MCP wrappers are `_plugin_discoverable = False` with default scope `{"core"}`, so MCP tools never enter a subagent's registry.

## MCP `incoming_messages` Must Be Drained

The MCP SDK routes server-side notifications and stdout-parsing exceptions into a capacity-0 anyio channel (`session.incoming_messages`). If nobody reads it, the SDK's `_receive_loop` blocks forever on the next `send()`, wedging the whole session — every subsequent `call_tool` waits on a response that never arrives. `mcp.py` starts a background drainer (`_start_incoming_drainer`) tied to each server's `AsyncExitStack` to keep the channel consumed. Tool-call responses travel a separate id-keyed stream, so draining never steals a result.
