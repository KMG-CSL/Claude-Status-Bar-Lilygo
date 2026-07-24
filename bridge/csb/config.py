"""Config, data dir, and platform constants for the bridge."""

import json
import os
import platform
import sys

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
HOME = os.path.expanduser("~")

# CLAUDE_CONFIG_DIR relocates the whole ~/.claude tree (all platforms)
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")

# bridge/ — the directory that holds claude_bar_bridge.py, one level above csb/
_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_roots():
    roots = [os.path.join(CLAUDE_DIR, "projects")]   # Claude Code, all platforms
    if IS_WINDOWS:
        roots.append(os.path.join(APPDATA, "Claude", "local-agent-mode-sessions"))
        # The MSIX-packaged Claude Desktop app virtualizes its AppData writes to
        # AppData\Local\Packages\Claude_*\LocalCache\Roaming\Claude\...
        pkg_root = os.path.join(LOCALAPPDATA, "Packages")
        if os.path.isdir(pkg_root):
            for name in os.listdir(pkg_root):
                if "claude" in name.lower():
                    roots.append(os.path.join(
                        pkg_root, name, "LocalCache", "Roaming", "Claude",
                        "local-agent-mode-sessions"))
    elif IS_MAC:
        roots.append(os.path.join(HOME, "Library", "Application Support",
                                  "Claude", "local-agent-mode-sessions"))
    else:  # Linux
        roots.append(os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
            "Claude", "local-agent-mode-sessions"))
    return roots


DEFAULT_CONFIG = {
    "roots": default_roots(),
    "port": "",                  # "" = auto-detect (ESP32-S3 native USB)
    "baud": 115200,
    "max_sessions": 8,
    "active_window_min": 30,     # sessions modified within N minutes are shown
    "idle_after_s": 120,         # no file writes for this long -> idle/done
    "wait_tool_s": 20,           # pending tool call older than this -> "waiting on you"
    "context_limit": 200000,
    "done_after_s": 30,              # no new events for this long -> turn is done
    "question_after_s": 12,          # ...but a trailing "?" flips to wait this fast
    "est_cap_5h_tokens": 8000000,    # only used if OAuth usage API unavailable
    "est_cap_7d_tokens": 60000000,
    "send_interval_s": 1.0,
    # input bindings, pushed to the display on connect. Actions:
    # "cycle" (next/prev session), "page" (toggle status/usage),
    # "usage" (alias of page), "flip" (rotate 180), "none"
    "input": {
        "tap": "cycle",
        "swipe": "cycle",
        "hold": "usage",
        "boot_short": "cycle",
        "boot_long": "flip",
    },
}


def data_dir():
    """Where config.json / logo.bin live. Next to the scripts normally;
    a per-user app-data dir when running as a packaged exe."""
    if getattr(sys, "frozen", False):
        if IS_WINDOWS:
            d = os.path.join(APPDATA, "ClaudeStatusBar")
        elif IS_MAC:
            d = os.path.join(HOME, "Library", "Application Support", "ClaudeStatusBar")
        else:
            d = os.path.join(
                os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
                "ClaudeStatusBar")
        os.makedirs(d, exist_ok=True)
        return d
    return _BRIDGE_DIR


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(data_dir(), "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            inp = dict(DEFAULT_CONFIG["input"])
            inp.update(user.get("input") or {})
            cfg.update(user)
            cfg["input"] = inp
        except Exception as e:
            print(f"[warn] bad config.json ignored: {e}")
    return cfg


def input_cfg_packet(cfg):
    """One-line input-binding packet, sent to the display on connect."""
    i = cfg["input"]
    pkt = {"t": "cf", "tap": i.get("tap", "cycle"),
           "swipe": i.get("swipe", "cycle"), "hold": i.get("hold", "usage"),
           "bshort": i.get("boot_short", "cycle"),
           "blong": i.get("boot_long", "flip")}
    return json.dumps(pkt, separators=(",", ":")) + "\n"
