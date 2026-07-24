"""Claude Status Bar bridge internals.

`claude_bar_bridge.py` is the public facade; it re-exports everything an
external caller (the desktop app, scripts) should touch. Modules here may
be rearranged between releases — import from the facade instead.
"""
