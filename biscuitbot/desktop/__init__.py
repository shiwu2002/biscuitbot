"""Headless desktop gateway runtime for biscuitbot.

Launches the gateway in a background daemon thread so the Tauri desktop shell
can serve the built-in WebUI inside its system WebView.  No window is opened
here; the shell renders the UI.
"""
