"""KVM v1: focus the terminal of a session picked on the display.

The device sends {"t":"focus","sl":<slot>} upstream on its focus gesture;
the bridge resolves slot -> session -> claude PID -> tty and asks the
terminal to foreground that exact tab (docs/kvm-design.md).

Both triggers (device long-press, desktop-app minimap click) arrive here
through the same message, so adapter work lands once and serves both.

v1 scope, deliberately minimal:
- adapter: iTerm2 only (AppleScript by tty) — the design doc's per-session
  env detection and the tmux/linux adapters come later. "auto" therefore
  resolves to nothing off macOS: KVM reports itself off once at startup
  rather than failing per click on a platform it can't serve yet.
- PID binding: exact when the session arrived through a hook, which posts
  the claude process's own pid (hooks.py gen 2). Sessions seen only via
  transcript tailing, or whose recorded pid has since died, fall back to
  matching claude processes by cwd; a cwd shared by several sessions is
  ambiguous, so we pick the newest and say so in the log.
Every attempt logs its outcome; a failure never retries (single shot).
"""

import os
import subprocess
import sys
import time

from .config import log

# Adapters that exist today. "auto" maps to the one this platform can use;
# kvm-design.md lists the rest (tmux, vscode, linux_generic, exec) as the
# slots they drop into.
ADAPTERS = ("iterm2",)
_AUTO_BY_PLATFORM = {"darwin": "iterm2"}
_announced = set()


def adapter_for(cfg):
    """-> adapter name for this config/platform, or "" when KVM is off.

    "none" disables it outright; "auto" (the default) picks by platform and
    yields "" where no adapter exists yet. An explicitly pinned adapter is
    honored even on a platform we would not have chosen it for — pinning is
    the user overriding our guess, not asking us to re-guess.
    """
    name = (cfg.get("focus") or {}).get("adapter", "auto")
    if name == "none":
        return ""
    if name == "auto":
        return _AUTO_BY_PLATFORM.get(sys.platform, "")
    return name


def enabled(cfg):
    """True when a focus request would actually be attempted."""
    return bool(adapter_for(cfg))


def announce(cfg):
    """Log the resolved adapter once per process, so 'why did nothing
    happen when I clicked' is answerable from the console."""
    ad = adapter_for(cfg)
    key = ad or "off"
    if key in _announced:
        return
    _announced.add(key)
    if ad:
        log("focus", f"KVM enabled (adapter {ad})")
    else:
        cfgd = (cfg.get("focus") or {}).get("adapter", "auto")
        why = ("disabled by config" if cfgd == "none" else
               f"no adapter for this platform ({sys.platform}) yet")
        log("focus", f"KVM off: {why}. Set focus.adapter in config.json "
                     "to override (see config.example.json).")

# macOS first-AppleEvent to iTerm2 pops a one-time Automation permission
# dialog — expected, approve it once.
_ITERM_SCRIPT = """
on run argv
  set wanted to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is wanted then
            select t
            tell w to select
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "no-session"
end run
"""


def _run(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def claude_pids_by_cwd(cwd, runner=_run):
    """All claude-process PIDs whose cwd matches, newest started last."""
    out = runner(["ps", "-axo", "pid=,lstart=,comm=,command="]).stdout
    pids = []
    for ln in out.splitlines():
        parts = ln.split(None, 6)
        if len(parts) < 7:
            continue
        cmd = parts[6]
        base = os.path.basename(cmd.split()[0])
        if base != "claude" or "--version" in cmd or "--help" in cmd:
            continue
        pids.append(parts[0])
    matched = []
    for pid in pids:
        lsof = runner(["lsof", "-a", "-d", "cwd", "-Fn", "-p", pid]).stdout
        for l in lsof.splitlines():
            if l.startswith("n") and l[1:] == cwd:
                matched.append(pid)
    return matched


def tty_of(pid, runner=_run):
    tty = runner(["ps", "-o", "tty=", "-p", pid]).stdout.strip()
    if not tty or tty == "??":
        return ""
    return tty if tty.startswith("/dev/") else "/dev/" + tty


def focus_tty(tty, runner=_run):
    r = runner(["osascript", "-e", _ITERM_SCRIPT, tty], timeout=10)
    ok = r.returncode == 0 and "ok" in (r.stdout or "")
    return ok, (r.stdout or r.stderr or "").strip()


def focus_slot(core, sl, runner=_run):
    """Resolve a display slot to a terminal and focus it. Logs outcome."""
    if not enabled(core.cfg):
        announce(core.cfg)
        return False
    sid = None
    for s, e in core.slots.entries.items():
        if e.get("slot") == sl:
            sid = s
            break
    ses = None
    if sid:
        for s in core.sessions.values():
            if s.session_id == sid:
                ses = s
                break
    if ses is None or not ses.cwd:
        log("focus", f"slot {sl}: no session/cwd resolved")
        return False
    t0 = time.time()
    pids = []
    if ses.claude_pid:
        try:                     # exact binding from hook ppid, if alive
            os.kill(ses.claude_pid, 0)
            pids = [str(ses.claude_pid)]
        except (OSError, ProcessLookupError):
            pass                 # stale pid: fall through to cwd scan
    if not pids:
        pids = claude_pids_by_cwd(ses.cwd, runner=runner)
    cwd = ses.cwd
    while not pids and cwd.count("/") > 2:
        # older transcripts may have recorded a subdirectory the shell
        # visited; the process sits somewhere up the tree
        cwd = os.path.dirname(cwd)
        pids = claude_pids_by_cwd(cwd, runner=runner)
    if not pids:
        log("focus", f"{ses.project}: no claude process in {ses.cwd} "
            "(or its parents)")
        return False
    pid = pids[-1]
    if len(pids) > 1:
        log("focus", f"{ses.project}: {len(pids)} claude PIDs share this cwd — "
            f"guessing newest ({pid}). Exact binding needs this session's "
            "hooks: python -m csb.hooks status")
    tty = tty_of(pid, runner=runner)
    if not tty:
        log("focus", f"{ses.project}: pid {pid} has no tty")
        return False
    ok, detail = focus_tty(tty, runner=runner)
    log("focus", f"{ses.project} pid={pid} tty={tty} -> "
        f"{'focused' if ok else 'FAILED: ' + detail} "
        f"({int((time.time() - t0) * 1000)}ms)")
    return ok
