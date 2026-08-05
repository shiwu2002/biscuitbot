---
name: system-io
tier: system
description: System-level IO on the host OS — simulate keyboard/mouse input, read/write the clipboard, list USB devices, and read/write serial ports. Use for GUI automation (click, type, hotkeys), clipboard integration, and hardware IO when shell commands are insufficient. Disabled by default; enable via tools.system_io.enable. This is a system-level skill that operates directly on the host machine.
always: false
---

# System-Level IO

Directly operate the host operating system: keyboard/mouse simulation, clipboard, USB devices, and serial ports.

## When to use

<rule>
**Prefer the system_io tool over shell commands for input simulation and hardware IO.** `xdotool`/`osascript` snippets via exec are brittle and platform-specific; system_io abstracts them.
</rule>

<rule>
**GUI automation only.** Use when you must drive a real GUI app (click a button, type into a field, send a hotkey) that has no CLI or API.
</rule>

<rule>
**Clipboard as a bridge.** Read/write the clipboard to exchange data with GUI apps that don't expose programmatic access.
</rule>

<rule>
**Hardware IO.** Enumerate USB devices or talk to serial peripherals (Arduino, sensors, modems) when no higher-level tool fits.
</rule>

## How to use

1. **Confirm the tool is enabled.** It is off by default. If `system_io` is not registered, tell the user to set `tools.system_io.enable = true` in config. The pynput/pyserial dependencies are bundled with hczkbot, so no extra install is needed for full keyboard/mouse/serial support.
2. **Pick the action** from the table below and pass it as `action`.
3. **Read docs/system_io.md** for per-action parameters and platform requirements before first use in a session.

## Actions

| Action | Category | What it does |
|--------|----------|--------------|
| `clipboard_read` | read | Read system clipboard text |
| `clipboard_write` | write | Write text to system clipboard |
| `key_tap` | write | Tap a key or chord, e.g. `cmd+c`, `ctrl+shift+esc`, `enter` |
| `key_type` | write | Type a full text string via the keyboard |
| `mouse_move` | write | Move the mouse to (x, y) |
| `mouse_click` | write | Click at (x, y) with left/right/middle button |
| `mouse_scroll` | write | Scroll the mouse wheel (dx, dy) |
| `usb_list` | read | List connected USB devices |
| `serial_list` | read | List available serial ports |
| `serial_write` | write | Write data to a serial port |
| `serial_read` | write | Read data from a serial port |

## Examples

```
system_io(action="clipboard_read")
system_io(action="clipboard_write", text="hello world")
system_io(action="key_tap", keys="cmd+c")
system_io(action="key_type", text="Hello")
system_io(action="mouse_click", x=500, y=300, button="left")
system_io(action="mouse_scroll", scroll_dy=-3)
system_io(action="usb_list")
system_io(action="serial_list")
system_io(action="serial_write", port="/dev/ttyUSB0", baudrate=115200, text="AT\r\n")
system_io(action="serial_read", port="/dev/ttyUSB0", baudrate=115200, bytes_to_read=64)
```

## Safety

<rule>
**Warn before any write action.** Keyboard/mouse/clipboard/serial writes mutate the host OS in real time and can affect the user's focused app. State what you're about to do first.
</rule>

<rule>
**Never type or click without confirming the target.** A wrong coordinate or keystroke can trigger irreversible actions in the user's foreground app.
</rule>

<rule>
**Don't hold serial ports open.** Each serial_read/serial_write opens, operates, and closes the port. Don't try to keep a session.
</rule>

<rule>
**Respect allow_actions.** If an action is blocked by the configured allowlist, inform the user rather than retrying.
</rule>

## Platform notes

- **macOS**: keyboard/mouse simulation and screen recording require Accessibility & Screen Recording permissions (System Settings → Privacy & Security). Clipboard uses `pbpaste`/`pbcopy`; USB uses `system_profiler`.
- **Linux**: needs `DISPLAY` and an accessible X server. Clipboard uses `xclip`/`xsel`; USB uses `lsusb`; keyboard/mouse fall back to `xdotool`.
- **Windows**: keyboard/mouse require `pynput` (no osascript/xdotool fallback). Clipboard uses PowerShell; USB uses `Get-PnpDevice`.
- **Serial** read/write always requires `pyserial` (optional dependency).

## Anti-patterns

<rule>
**Don't use system_io for things shell can do.** File operations, process management, network calls belong to exec/filesystem/web tools. system_io is for input simulation and hardware IO.
</rule>

<rule>
**Don't spam input.** Batch key_type for text rather than many key_tap calls; one click per target.
</rule>

<rule>
**Don't assume coordinates.** Screen resolutions vary. If you need a pixel location, take a screenshot first to locate the target.
</rule>

## Related tools

| Need | Use |
|------|-----|
| Run a shell command | `exec` tool |
| Capture the screen | `screenshot` tool |
| Read/write files | `file` tool |
| See what's on screen before clicking | `screenshot` then `system_io(mouse_click)` |
