"""Hooks-push pipeline (Item 1): a localhost-only HTTP listener that
receives Claude Code hook events, plus the opt-in installer CLI that
merges the hook entries into a settings.json.

Runtime never touches ~/.claude/: the listener writes only its port file
inside our own data dir, and settings.json is modified exclusively by the
explicit `install` command the user runs:

    python -m csb.hooks install|uninstall|status [--settings PATH]
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import CLAUDE_DIR, data_dir, debug, load_config, log

# Marker name inside every installed hook command: install/uninstall stay
# idempotent string-contains checks (ClaudeBar / ccstatusline pattern),
# and entries additionally carry a "_tag" for clean structured removal.
MARKER = "__claudestatusbar_hook"
TAG = "claudestatusbar"

# If the bridge ever spawns its own `claude` probe, its cwd must end with
# this suffix so its hook events are dropped at the listener (ClaudeBar
# #172 self-probe guard). Unused until a probe exists.
SELF_PROBE_CWD_SUFFIX = "csb-self-probe"

# Exactly these 8 official events — no unofficial names (TaskCompleted /
# SubagentStart are NOT registered). PreToolUse/Notification matchers give
# instant waiting states (claude-notifications-go).
HOOK_MATCHERS = (
    ("SessionStart", None),
    ("SessionEnd", None),
    ("UserPromptSubmit", None),
    ("Stop", None),
    ("SubagentStop", None),
    ("PreToolUse", "ExitPlanMode|AskUserQuestion"),
    ("PostToolUse", None),
    ("Notification", "permission_prompt"),
)

PAYLOAD_FIELDS = ("session_id", "transcript_path", "cwd",
                  "hook_event_name", "tool_name")


def default_port_file(cfg=None):
    return (cfg or {}).get("hook_port_file") or \
        os.path.join(data_dir(), "hook-port")


# ---------------------------------------------------------------- listener

class HookListener:
    """127.0.0.1-only ThreadingHTTPServer. Parsed events (the 5 payload
    fields + arrival "ts") land in .queue for BridgeCore.step() to drain.
    Preferred port from cfg["hook_port"]; on OSError falls back to an
    auto port (bind 0). The ACTUAL port is written to the port file so
    the installed hook command can read it at run time."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.queue = queue.Queue()
        handler = self._handler_class()
        preferred = int(cfg.get("hook_port", 45732) or 0)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", preferred), handler)
        except OSError:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.port_file = default_port_file(cfg)
        self._write_port_file()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="csb-hooks", daemon=True)
        self.thread.start()

    def _handler_class(self):
        q = self.queue

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = None
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(n) or b"{}")
                except Exception as e:
                    debug("hooks", f"bad hook payload ignored: {e}")
                if isinstance(payload, dict):
                    ev = {k: payload.get(k) for k in PAYLOAD_FIELDS}
                    ev["ts"] = time.time()
                    cwd = (ev.get("cwd") or "").rstrip("/\\")
                    if cwd.endswith(SELF_PROBE_CWD_SUFFIX):
                        debug("hooks", "self-probe event dropped")
                    else:
                        q.put(ev)
                # respond after the enqueue so a client that saw 204 knows
                # the event is in the queue (tests rely on this ordering)
                self.send_response(204)
                self.end_headers()

            def log_message(self, fmt, *args):   # stdlib access log
                debug("hooks", fmt % args)

        return Handler

    def _write_port_file(self):
        tmp = f"{self.port_file}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(self.port))
        os.replace(tmp, self.port_file)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        try:
            os.remove(self.port_file)
        except OSError:
            pass


# ---------------------------------------------------------------- installer

def resolved_port_file(port_file=None):
    """CLI-side port-file resolution: an explicit --port-file wins, else
    the same load_config() the running bridge uses — so a hook_port_file
    set in config.json or via CSB_HOOK_PORT_FILE ends up baked into the
    installed hook command / read by `status`, matching where
    HookListener actually writes its port."""
    return port_file or default_port_file(load_config())


def hook_command(port_file=None):
    """The settings.json hook command. Reads the port from the port file
    at hook run time ($(cat ...)) so bridge restarts on a different port
    keep working; reads stdin in the foreground (a backgrounded pipeline
    gets /dev/null stdin under POSIX sh) then backgrounds the curl so
    Claude never blocks, even with the bridge down."""
    pf = resolved_port_file(port_file)
    return (MARKER + "(){ p=$(cat); (curl -s -m 2 -X POST "
            f"\"http://127.0.0.1:$(cat '{pf}' 2>/dev/null)/hook\" "
            "--data-binary \"$p\" >/dev/null 2>&1 &); }; " + MARKER)


def _read_settings(path):
    """-> (parsed_dict_or_None, raw_text_or_None). Missing file is a
    valid empty config; unparseable JSON refuses (never clobber)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        return json.loads(raw or "{}"), raw
    except Exception as e:
        log("hooks", f"refusing to touch unparseable {path}: {e}")
        return None, None


def _write_settings(path, settings):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)


def _strip_ours(settings):
    """Remove entries tagged _tag=claudestatusbar or whose command contains
    the marker — nothing else. User hooks and unknown keys pass through
    untouched. -> True if anything was removed."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict):
                if g.get("_tag") == TAG:
                    changed = True
                    continue
                inner = g.get("hooks")
                if isinstance(inner, list):
                    survivors = [h for h in inner
                                 if not (isinstance(h, dict)
                                         and MARKER in str(h.get("command", "")))]
                    if len(survivors) != len(inner):
                        changed = True
                        if not survivors:
                            continue      # the group was entirely ours
                        g = dict(g)
                        g["hooks"] = survivors
            kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if changed and not hooks:
        del settings["hooks"]
    return changed


def install(settings_path, port_file=None):
    """Merge our 8 tagged hook entries into settings.json (idempotent:
    remove-tagged-then-add). Opt-in CLI only — never called at runtime."""
    if sys.platform == "win32":
        log("hooks", "install refused: the hook command is POSIX-only; "
                     "Windows support is deferred (settings.json untouched)")
        return 2
    settings, raw = _read_settings(settings_path)
    if settings is None:
        return 1
    bak = settings_path + ".csb-bak"
    if raw is not None and not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(raw)                 # pre-write backup, left once
    _strip_ours(settings)
    cmd = hook_command(port_file)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        log("hooks", f"refusing: 'hooks' in {settings_path} is not an object")
        return 1
    for event, matcher in HOOK_MATCHERS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            log("hooks", f"skipping {event}: existing value is not a list")
            continue
        entry = {"_tag": TAG}
        if matcher:
            entry["matcher"] = matcher
        entry["hooks"] = [{"type": "command", "command": cmd}]
        groups.append(entry)
    _write_settings(settings_path, settings)
    log("hooks", f"installed {len(HOOK_MATCHERS)} hook entries "
                 f"into {settings_path}")
    return 0


def uninstall(settings_path):
    """Remove only our tagged/marker entries; everything else untouched."""
    settings, raw = _read_settings(settings_path)
    if settings is None:
        return 1
    if raw is None:
        log("hooks", f"{settings_path} does not exist; nothing to remove")
        return 0
    if _strip_ours(settings):
        _write_settings(settings_path, settings)
        log("hooks", f"removed claudestatusbar hooks from {settings_path}")
    else:
        log("hooks", f"no claudestatusbar hooks in {settings_path}")
    return 0


def installed_events(settings_path):
    """-> sorted list of events that currently carry one of our entries."""
    settings, _raw = _read_settings(settings_path)
    if not settings:
        return []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    out = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if isinstance(g, dict) and (
                    g.get("_tag") == TAG
                    or any(isinstance(h, dict)
                           and MARKER in str(h.get("command", ""))
                           for h in g.get("hooks") or [])):
                out.append(event)
                break
    return sorted(out)


def status(settings_path, port_file=None):
    if sys.platform == "win32":
        print("unsupported platform (hook command is POSIX-only)")
        return 0
    events = installed_events(settings_path)
    if events:
        print(f"installed ({len(events)} events) in {settings_path}: "
              + ", ".join(events))
    else:
        print(f"not installed in {settings_path}")
    pf = resolved_port_file(port_file)
    try:
        with open(pf, "r", encoding="utf-8") as f:
            print(f"listener port {f.read().strip()} (port file {pf})")
    except OSError:
        print(f"no port file at {pf} (bridge not running?)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m csb.hooks",
        description="Opt-in Claude Code hook install for the status bar "
                    "bridge (merges tagged entries into settings.json).")
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    ap.add_argument("--settings",
                    default=os.path.join(CLAUDE_DIR, "settings.json"),
                    help="settings.json path (default: %(default)s)")
    ap.add_argument("--port-file", default="",
                    help="port file baked into the hook command (default: "
                         "the bridge config's hook_port_file, else "
                         "<data dir>/hook-port)")
    args = ap.parse_args(argv)
    if args.action == "install":
        return install(args.settings, port_file=args.port_file or None)
    if args.action == "uninstall":
        return uninstall(args.settings)
    return status(args.settings, port_file=args.port_file or None)


if __name__ == "__main__":
    sys.exit(main())
