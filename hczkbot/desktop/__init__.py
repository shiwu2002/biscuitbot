"""Desktop application shell for hczkbot.

Launches the gateway in a background daemon thread and presents a native
window via pywebview.  The window loads the built-in WebUI from the gateway's
HTTP server, giving the full browser experience inside a native desktop frame.

Usage::

    from hczkbot.desktop.app import run_desktop
    run_desktop(config)
"""
