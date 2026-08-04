"""Config, data dir, env overrides, logging, and platform constants."""

import json
import os
import platform
import sys
from datetime import datetime


def log(tag, msg):
    """Timestamped line to stdout (12-factor: logs are an event stream)."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] [{tag}] {msg}")


def debug_enabled():
    return os.environ.get("CSB_DEBUG", "") not in ("", "0")


def debug(tag, msg):
    """Diagnostics that are normally swallowed; CSB_DEBUG=1 unmutes them."""
    if debug_enabled():
        log(tag, msg)

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
    # self-probe filter: project-dir basename substrings to never display —
    # sessions spawned by tooling in scratch dirs (Claude Code scratchpads
    # live under /tmp/claude-* or /private/tmp/claude-*)
    "ignore_projects": ["-tmp-claude-"],
    # session entrypoints to never display: "sdk-cli" = claude -p one-shots
    # and SDK-driven runs (interactive sessions record "cli")
    "ignore_entrypoints": ["sdk-cli"],
    "port": "",                  # "" = auto-detect (ESP32-S3 native USB)
    "baud": 115200,
    "max_sessions": 8,
    "active_window_min": 30,     # sessions modified within N minutes are shown
    "idle_after_s": 120,         # no real events for this long -> idle/done
    "wait_follow_stale_s": 300,  # a wait stops winning auto-follow once this
                                 # stale (it keeps its cell + banner regardless)
    "wait_tool_s": 20,           # legacy alias of approval_silence_s (kept for old configs)
    "approval_silence_s": 20,    # pending tool + this much write-silence -> "needs approval"
    "approval_confirm_s": 0.5,   # poll cadence while within 1s of the approval flip
    "context_limit": 200000,
    "done_after_s": 30,              # no new events for this long -> turn is done
    "question_after_s": 12,          # ...but a trailing "?" flips to wait this fast
    "est_cap_5h_tokens": 8000000,    # only used if OAuth usage API unavailable
    "est_cap_7d_tokens": 60000000,
    "send_interval_s": 1.0,
    "subagent_live_s": 15,           # subagent counts as live only with events this
                                     # recent (Stargx liveness; was 90 — finished
                                     # helpers lingered in sa for a minute and a half)
    "subagent_cache_s": 10,          # how often to re-scan subagent dirs
    "alert_cooldown_s": 86400,       # rate-limit alert re-fire suppression window
    "hooks_enabled": True,           # localhost hook listener (install stays opt-in CLI)
    "hook_port": 45732,              # preferred listener port (0 = pure auto)
    "hook_port_file": "",            # "" = data_dir()/hook-port
    "hook_fresh_s": 900,             # hook edges trusted while events this recent
    "statusline_ttl_s": 600,         # statusline captures older than this are ignored
    "statusline_dir": "",            # "" = data_dir()/statusline
    "slots_file": "",                # "" = data_dir()/slots.json (stable slot letters)
    # opt-in chime on a session's transition into wait / done: each value
    # an afplay(mac)/paplay(linux)-able sound path, "" = silent. One sound
    # per 10s globally, never on the first packet after startup.
    "chime": {
        "wait": "",
        "done": "",
    },
    # KVM: raise a session's terminal from the display (long-press with
    # input.hold="focus") or from the desktop app (minimap cell click).
    # "auto" = pick an adapter for this platform: macOS -> iterm2,
    # Linux -> tmux (selects the pane when the session is under tmux,
    # raises the window either way). Windows has none yet, so KVM stays
    # off there and says so. "none" turns it off everywhere; when off, a
    # minimap click selects the session instead.
    "focus": {
        "adapter": "auto",
    },
    # input bindings, pushed to the display on connect. Actions:
    # "cycle" (next/prev session), "page" (toggle status/usage),
    # "usage" (alias of page), "flip" (rotate 180),
    # "focus" (KVM: raise that session's terminal — macOS/iTerm2 only
    # today, a no-op with a logged reason elsewhere), "none".
    # Defaults stay cross-platform; the KVM layout is
    # hold=focus + boot_short=usage (see docs/kvm-design.md).
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
    a per-user app-data dir when running as a packaged exe.
    CSB_DATA_DIR overrides both (and keeps tests out of the real dirs)."""
    env = os.environ.get("CSB_DATA_DIR")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
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


def _coerce(raw):
    """Env values keep config types: JSON if it parses, raw string if not."""
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _normalize_roots(roots):
    """Accept the natural single-path forms for "roots" — a plain string
    (optionally os.pathsep-separated, PATH-style) — as well as the canonical
    list. Without this, iterating a bare string yields single characters and
    os.walk("/") scans the whole filesystem."""
    if isinstance(roots, str):
        return [p for p in (s.strip() for s in roots.split(os.pathsep)) if p]
    if isinstance(roots, list):
        return [str(p) for p in roots]
    log("warn", f"config 'roots' must be a path or list of paths, "
                f"got {type(roots).__name__}; using defaults")
    return list(DEFAULT_CONFIG["roots"])


def _apply_env_overrides(cfg):
    """CSB_<KEY> overrides any merged config key; CSB_INPUT_<KEY> the input
    sub-keys. Returns [(env_name, value)] for startup logging."""
    applied = []
    nested = ("input", "chime")
    for key in list(cfg):
        if key in nested:
            continue
        raw = os.environ.get("CSB_" + key.upper())
        if raw is not None:
            cfg[key] = _coerce(raw)
            applied.append(("CSB_" + key.upper(), cfg[key]))
    for group in nested:
        for key in list(cfg.get(group) or {}):
            env = f"CSB_{group.upper()}_{key.upper()}"
            raw = os.environ.get(env)
            if raw is not None:
                cfg[group][key] = _coerce(raw)
                applied.append((env, cfg[group][key]))
    return applied


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["input"] = dict(DEFAULT_CONFIG["input"])
    cfg["chime"] = dict(DEFAULT_CONFIG["chime"])
    cfg["focus"] = dict(DEFAULT_CONFIG["focus"])
    path = os.path.join(data_dir(), "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            unknown = sorted(k for k in user
                             if k not in DEFAULT_CONFIG and not k.startswith("_"))
            if unknown:
                log("warn", f"config.json has unknown keys (typo?): "
                            f"{', '.join(unknown)}")
            inp = dict(DEFAULT_CONFIG["input"])
            inp.update(user.get("input") or {})
            ch = dict(DEFAULT_CONFIG["chime"])
            ch.update(user.get("chime") or {})
            fo = dict(DEFAULT_CONFIG["focus"])
            fo.update(user.get("focus") or {})
            cfg.update(user)
            cfg["input"] = inp
            cfg["chime"] = ch
            cfg["focus"] = fo
            # honor a user-tuned legacy wait_tool_s as the approval debounce
            # unless the new key was set explicitly
            if "wait_tool_s" in user and "approval_silence_s" not in user:
                cfg["approval_silence_s"] = user["wait_tool_s"]
        except Exception as e:
            log("warn", f"bad config.json ignored: {e}")
    for name, val in _apply_env_overrides(cfg):
        log("config", f"env override {name}={val!r}")
    cfg["roots"] = _normalize_roots(cfg["roots"])
    return cfg


def input_cfg_packet(cfg):
    """One-line input-binding packet, sent to the display on connect."""
    i = cfg["input"]
    pkt = {"t": "cf", "tap": i.get("tap", "cycle"),
           "swipe": i.get("swipe", "cycle"), "hold": i.get("hold", "usage"),
           "bshort": i.get("boot_short", "cycle"),
           "blong": i.get("boot_long", "flip")}
    return json.dumps(pkt, separators=(",", ":")) + "\n"
