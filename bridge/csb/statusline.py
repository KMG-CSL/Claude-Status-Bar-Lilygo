"""Statusline pass-through collector (Item 3, ccburn + Maciek hardening).

Claude Code >= 2.1.80 pushes a JSON status payload (rate_limits,
context_window, cost, model, session_id, effort) to the configured
statusLine command on every refresh. The collector is a tiny pass-through
inserted in front of the user's own statusline command:

    python -m csb.statusline collect        # stdin -> capture file + stdout

It echoes stdin byte-identical (the user's statusline keeps rendering
exactly as before), then atomically snapshots the fields we care about to
a per-session drop file the bridge reads at packet-build time. Any
collector failure is swallowed after the echo — it can never break the
user's statusline.

Install is opt-in CLI only, mirroring csb.hooks — never automatic, never
against the real ~/.claude in tests:

    python -m csb.statusline install|uninstall|status [--settings PATH]

Read-side hardening (Maciek official.py): captures older than
statusline_ttl_s are ignored, used_percentage > 101 is dropped (leak bug
claude-code#52326), a percentage is treated as absent once now >=
resets_at (rollover ghost), and a tombstone capture suppresses itself.
"""

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time

from .config import CLAUDE_DIR, data_dir, debug, load_config, log
from .fmt import fmt_countdown, parse_ts
from .limits import is_expired

MARKER = "__claudestatusbar_statusline"

# capture filenames come from the payload's session_id — never let a
# hostile/broken id escape the capture dir
_SID_SAFE = re.compile(r"[^A-Za-z0-9._-]")

_BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def capture_dir(cfg=None):
    return (cfg or {}).get("statusline_dir") or \
        os.path.join(data_dir(), "statusline")


# --------------------------------------------------------------- collector

def snapshot(payload, dir_path, now=None):
    """Write the per-session capture file atomically (pid+thread-unique
    tmp + os.replace, safe under concurrent writers). -> path or None."""
    if now is None:
        now = time.time()
    sid = _SID_SAFE.sub("_", str(payload.get("session_id") or ""))
    if not sid.strip("._"):
        return None
    cw = payload.get("context_window")
    effort = payload.get("effort")
    cap = {
        "ts": now,
        "session_id": sid,
        "rate_limits": payload.get("rate_limits"),
        "context_used_pct": cw.get("used_percentage")
        if isinstance(cw, dict) else None,
        "cost": payload.get("cost"),
        "model": payload.get("model"),
        "effort": effort.get("level")
        if isinstance(effort, dict) else effort,
    }
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, sid + ".json")
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cap, f)
    os.replace(tmp, path)
    return path


def collect(stdin_bytes, dir_path, quiet=False, out=None):
    """Echo stdin verbatim (unless quiet), then try to capture. The echo
    always happens first and capture errors are swallowed — the user's
    statusline output must stay byte-identical no matter what."""
    if out is None:
        out = sys.stdout.buffer
    if not quiet:
        out.write(stdin_bytes)
        out.flush()
    try:
        snapshot(json.loads(stdin_bytes.decode("utf-8", "replace")), dir_path)
    except Exception as e:
        debug("statusline", f"capture failed: {e}")


# ------------------------------------------------------------------ reader

def read_capture(path, now, ttl_s=600):
    """-> hardened capture dict or None (missing / unparseable / older
    than the TTL / tombstoned). context_used_pct > 101 is dropped."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cap = json.load(f)
    except Exception:
        return None
    if not isinstance(cap, dict) or cap.get("tombstone"):
        return None
    ts = cap.get("ts")
    if not isinstance(ts, (int, float)) or now - ts >= ttl_s:
        return None
    pct = cap.get("context_used_pct")
    if not isinstance(pct, (int, float)) or not (0 <= pct <= 101):
        cap["context_used_pct"] = None
    return cap


def parse_rate_limits(rl, now):
    """rate_limits block -> {"p5","p7","r5","r7"} us fields, or None when
    no bucket is usable (enterprise accounts ship no rate_limits at all —
    the caller falls back to the OAuth path). Per-bucket hardening:
    used_percentage > 101 dropped, pct absent once now >= resets_at,
    tombstoned buckets suppressed."""
    if not isinstance(rl, dict):
        return None
    out, usable = {}, False
    for key, tag in (("five_hour", "5"), ("seven_day", "7")):
        p, r = -1, ""
        blk = rl.get(key)
        if isinstance(blk, dict) and not blk.get("tombstone"):
            resets = blk.get("resets_at")
            if isinstance(resets, str):
                resets = parse_ts(resets)
            elif not isinstance(resets, (int, float)):
                resets = None
            expired = resets is not None and is_expired(resets, now)
            pct = blk.get("used_percentage")
            if isinstance(pct, (int, float)) and 0 <= pct <= 101 \
                    and not expired:
                p = min(100, int(round(pct)))
                usable = True
                if resets:
                    r = fmt_countdown(resets - now)
        out["p" + tag], out["r" + tag] = p, r
    return out if usable else None


def latest_rate_limits(cfg, now=None):
    """Freshest capture (any session) with usable rate_limits ->
    {"p5","p7","r5","r7","est":False}, else None."""
    if now is None:
        now = time.time()
    d = capture_dir(cfg)
    ttl = cfg.get("statusline_ttl_s", 600)
    best = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for fn in names:
        if not fn.endswith(".json"):
            continue
        cap = read_capture(os.path.join(d, fn), now, ttl)
        if cap and isinstance(cap.get("rate_limits"), dict):
            if best is None or cap["ts"] > best["ts"]:
                best = cap
    if best is None:
        return None
    us = parse_rate_limits(best["rate_limits"], now)
    if us is None:
        return None
    us["est"] = False
    return us


def refresh(session, cfg, now=None):
    """Pull this session's capture (if any) onto the Session: sl_ts
    doubles as a liveness signal for the engine's freshness test, and
    sl_ctx_pct / sl_effort replace the Models-API context path at packet
    time. Reads only when the capture file's mtime moved."""
    if now is None:
        now = time.time()
    path = os.path.join(capture_dir(cfg), session.session_id + ".json")
    try:
        m = os.path.getmtime(path)
    except OSError:
        return
    if m == session._sl_mtime:
        return
    cap = read_capture(path, now, cfg.get("statusline_ttl_s", 600))
    if cap is None:
        return
    session._sl_mtime = m
    session.sl_ts = float(cap["ts"])
    session.sl_ctx_pct = cap.get("context_used_pct")
    session.sl_effort = str(cap.get("effort") or "")[:10]


# --------------------------------------------------------------- installer

def collector_command(dir_path=None, python=None):
    """The collect half of the statusLine pipeline. The capture dir is
    baked in at install time (like the hooks port-file path) so the
    collector — which runs inside Claude Code's environment, not the
    bridge's — drops files exactly where this bridge reads them."""
    py = python or sys.executable
    d = dir_path or capture_dir(load_config())
    return (f"CSB_TAG={MARKER} PYTHONPATH={shlex.quote(_BRIDGE_DIR)} "
            f"{shlex.quote(py)} -m csb.statusline collect "
            f"--dir {shlex.quote(d)}")


def _strip_command(cmd):
    """-> the user's original statusline command, with our collect
    segment removed ("" when the whole command was ours). The collect
    segment contains no pipe, so the original is everything after the
    first one."""
    if MARKER not in cmd:
        return cmd
    parts = cmd.split("|", 1)
    if len(parts) == 2 and MARKER not in parts[1]:
        return parts[1].strip()
    return ""


def install(settings_path, dir_path=None, python=None):
    """Wrap the configured statusLine command as `our-collect | cmd`
    (or install the collector alone, emitting empty output, when none is
    configured). Idempotent: reinstall replaces our old segment. Opt-in
    CLI only — never called at runtime."""
    if sys.platform == "win32":
        log("statusline", "install refused: the collector command is "
                          "POSIX-only; Windows support is deferred "
                          "(settings.json untouched)")
        return 2
    from .hooks import _read_settings, _write_settings
    settings, raw = _read_settings(settings_path)
    if settings is None:
        return 1
    bak = settings_path + ".csb-bak"
    if raw is not None and not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(raw)                 # pre-write backup, left once
    sl = settings.get("statusLine")
    sl = dict(sl) if isinstance(sl, dict) else {}
    original = _strip_command(str(sl.get("command") or ""))
    ours = collector_command(dir_path, python)
    sl["type"] = "command"
    sl["command"] = f"{ours} | {original}" if original else f"{ours} --quiet"
    settings["statusLine"] = sl
    _write_settings(settings_path, settings)
    what = f"wrapping {original!r}" if original else "standalone (quiet)"
    log("statusline", f"installed collector into {settings_path} ({what})")
    return 0


def uninstall(settings_path):
    """Restore the user's original statusLine command; remove the key
    entirely when the collector was installed standalone."""
    from .hooks import _read_settings, _write_settings
    settings, raw = _read_settings(settings_path)
    if settings is None:
        return 1
    sl = settings.get("statusLine")
    cmd = str(sl.get("command") or "") if isinstance(sl, dict) else ""
    if raw is None or MARKER not in cmd:
        log("statusline", f"no collector in {settings_path}; "
                          f"nothing to remove")
        return 0
    original = _strip_command(cmd)
    if original:
        sl = dict(sl)
        sl["command"] = original
        settings["statusLine"] = sl
    else:
        del settings["statusLine"]
    _write_settings(settings_path, settings)
    log("statusline", f"removed collector from {settings_path}")
    return 0


def status(settings_path, cfg=None):
    if sys.platform == "win32":
        print("unsupported platform (collector command is POSIX-only)")
        return 0
    from .hooks import _read_settings
    settings, _raw = _read_settings(settings_path)
    sl = (settings or {}).get("statusLine")
    cmd = str(sl.get("command") or "") if isinstance(sl, dict) else ""
    if MARKER in cmd:
        original = _strip_command(cmd)
        print(f"installed in {settings_path} "
              + (f"(wrapping: {original})" if original else "(standalone)"))
    else:
        print(f"not installed in {settings_path}")
    d = capture_dir(cfg if cfg is not None else load_config())
    try:
        n = len([f for f in os.listdir(d) if f.endswith(".json")])
        print(f"{n} capture(s) in {d}")
    except OSError:
        print(f"no capture dir at {d} (no statusline data yet)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m csb.statusline",
        description="Statusline pass-through collector for the status bar "
                    "bridge (capture install stays an explicit CLI step).")
    ap.add_argument("action",
                    choices=("collect", "install", "uninstall", "status"))
    ap.add_argument("--settings",
                    default=os.path.join(CLAUDE_DIR, "settings.json"),
                    help="settings.json path (default: %(default)s)")
    ap.add_argument("--dir", default="",
                    help="capture directory (default: the bridge config's "
                         "statusline_dir, else <data dir>/statusline)")
    ap.add_argument("--quiet", action="store_true",
                    help="collect only: emit no output (standalone install "
                         "with no wrapped statusline command)")
    args = ap.parse_args(argv)
    if args.action == "collect":
        d = args.dir or capture_dir(load_config())
        collect(sys.stdin.buffer.read(), d, quiet=args.quiet)
        return 0
    if args.action == "install":
        return install(args.settings, dir_path=args.dir or None)
    if args.action == "uninstall":
        return uninstall(args.settings)
    return status(args.settings)


if __name__ == "__main__":
    sys.exit(main())
