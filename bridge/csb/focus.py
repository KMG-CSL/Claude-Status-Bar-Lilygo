"""KVM v1: focus the terminal of a session picked on the display.

The device sends {"t":"focus","sl":<slot>} upstream on its focus gesture;
the bridge resolves slot -> session -> claude PID -> tty and asks the
terminal to foreground that exact tab (docs/kvm-design.md).

v1 scope, deliberately minimal:
- adapter: iTerm2 only (AppleScript by tty) — the design doc's per-session
  env detection and the tmux/linux adapters come later
- PID binding: exact when the session arrived through a hook, which posts
  the claude process's own pid (hooks.py gen 2). Sessions seen only via
  transcript tailing, or whose recorded pid has since died, fall back to
  matching claude processes by cwd; a cwd shared by several sessions is
  ambiguous, so we pick the newest and say so in the log.
Every attempt logs its outcome; a failure never retries (single shot).
"""

import os
import subprocess
import time

from .config import log

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
