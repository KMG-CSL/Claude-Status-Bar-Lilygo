"""KVM v1: focus the terminal of a session picked on the display.

The device sends {"t":"focus","sl":<slot>} upstream on its focus gesture;
the bridge resolves slot -> session -> claude PID -> tty and asks the
terminal to foreground that exact tab (docs/kvm-design.md).

Both triggers (device long-press, desktop-app minimap click) arrive here
through the same message, so adapter work lands once and serves both.

Adapter contract: adapter(ctx, runner) -> (ok, detail). ctx is the
per-session dict kvm-design.md specifies — pid, tty, cwd, session_id, env.
The env matters: a tty alone identifies a session under iTerm2, which owns
every one of its tabs, but on Linux the same tty may sit under tmux or a
bare shell, and only the process's own environment distinguishes them.

`auto` resolves to exactly ONE adapter — there is deliberately no
decline-and-fall-through chain. A window raise is not an alternative to a
tmux pane select, it is its companion (in the tmux case both must happen),
so a chain would either stop on first success or need a succeeded-but-
keep-going state. Adapters compose internally instead, by calling the
raise_window() helper, which is pointedly not an adapter: best effort, no
session identity, its own return. One adapter calling a utility is ordinary
code; adapters calling adapters would not be.

PID binding: exact when the session arrived through a hook, which posts the
claude process's own pid (hooks.py gen 2). Sessions seen only via transcript
tailing, or whose recorded pid has since died, fall back to matching claude
processes by cwd; a cwd shared by several sessions is ambiguous, so we pick
the newest and say so in the log.

Every attempt logs its outcome; a failure never retries (single shot).
"""

import os
import subprocess
import sys
import time

from .config import log

# name -> adapter function, populated at the bottom of this module once the
# adapters are defined. "auto" maps to the one this platform can use;
# kvm-design.md lists the rest (vscode, ghostty, exec) as slots to fill.
ADAPTERS = {}
_AUTO_BY_PLATFORM = {"darwin": "iterm2", "linux": "tmux"}
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


def env_of(pid, runner=_run):
    """-> the process's own environment as a dict, {} if unreadable.

    This is the per-session terminal detection kvm-design.md specifies: the
    claude process's env names its host (TMUX, TERM_PROGRAM, ...), so a
    mixed setup needs no configuration.
    """
    text = ""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return {}
    else:                       # macOS/BSD: ps eww prints it space-separated
        text = runner(["ps", "eww", "-o", "command=", "-p", str(pid)]).stdout
        text = text.replace(" ", "\0")
    env = {}
    for item in text.split("\0"):
        k, sep, v = item.partition("=")
        if sep and k and " " not in k:
            env[k] = v
    return env


def raise_window(pid, runner=_run):
    """Best-effort: bring the window hosting `pid` to the front. -> bool.

    Deliberately NOT an adapter — no session identity, no detail string, no
    promise. Adapters call it as a companion step; it is ordinary shared
    code, which is why there is no adapter-to-adapter recursion.

    X11 only. GNOME on Wayland blocks programmatic activation by design
    with no general replacement, so this quietly returns False there rather
    than pretending. A session in a bare Wayland shell therefore gets the
    pane select (if any) and no raise — less, not nothing.
    """
    if not sys.platform.startswith("linux"):
        return False
    for tool, args in (("wmctrl", ["-i", "-a"]), ("xdotool", ["windowactivate"])):
        try:
            if tool == "wmctrl":
                out = runner(["wmctrl", "-lp"]).stdout
                win = ""
                for ln in out.splitlines():
                    cols = ln.split(None, 3)
                    if len(cols) >= 3 and cols[2] == str(pid):
                        win = cols[0]
                        break
                if not win:
                    continue
                return runner(["wmctrl"] + args + [win]).returncode == 0
            out = runner(["xdotool", "search", "--pid", str(pid)]).stdout
            wins = [w for w in out.split() if w.strip()]
            if not wins:
                continue
            return runner(["xdotool"] + args + [wins[-1]]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def focus_iterm2(ctx, runner=_run):
    """iTerm2 owns every one of its tabs, so the tty alone identifies the
    session and AppleScript can select it directly."""
    r = runner(["osascript", "-e", _ITERM_SCRIPT, ctx["tty"]], timeout=10)
    ok = r.returncode == 0 and "ok" in (r.stdout or "")
    return ok, (r.stdout or r.stderr or "").strip()


def tmux_pane_for(tty, runner=_run):
    """-> "session:window.pane" whose pane_tty matches, or ""."""
    out = runner(["tmux", "list-panes", "-a", "-F",
                  "#{pane_tty} #{session_name}:#{window_index}.#{pane_index}"
                  ]).stdout
    for ln in out.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and parts[0] == tty:
            return parts[1].strip()
    return ""


def focus_tmux(ctx, runner=_run):
    """Linux: select the session's tmux pane, and raise its window.

    Named for its distinguishing capability, but it never declines. When the
    session is NOT under tmux there is no pane to select, so it does the
    window raise alone — less, not nothing. Both steps run in the tmux case
    because a selected pane inside a backgrounded window is still invisible.
    """
    pane, detail = "", []
    if ctx["env"].get("TMUX"):
        pane = tmux_pane_for(ctx["tty"], runner=runner)
        if pane:
            win = pane.rsplit(".", 1)[0]
            sel = runner(["tmux", "select-window", "-t", win])
            runner(["tmux", "select-pane", "-t", pane])
            runner(["tmux", "switch-client", "-t", win])
            if sel.returncode != 0:
                detail.append("pane select failed: "
                              + (sel.stderr or "").strip())
        else:
            detail.append(f"TMUX set but no pane owns {ctx['tty']}")
    raised = raise_window(ctx["pid"], runner=runner)
    if not raised:
        detail.append("window raise unavailable (X11 only; no-op on Wayland)")
    # honest partial: landing the user in the right window still counts,
    # but the shortfall is named rather than collapsed into a bare ok
    ok = bool(pane and not any(d.startswith("pane select failed") for d in detail)) \
        or raised
    return ok, "; ".join(detail)


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
    name = adapter_for(core.cfg)
    fn = ADAPTERS.get(name)
    if fn is None:
        log("focus", f"{ses.project}: no adapter named {name!r} "
            f"(have: {', '.join(sorted(ADAPTERS))})")
        return False
    ctx = {"pid": pid, "tty": tty, "cwd": ses.cwd,
           "session_id": ses.session_id, "env": env_of(pid, runner=runner)}
    ok, detail = fn(ctx, runner=runner)
    log("focus", f"{ses.project} via {name} pid={pid} tty={tty} -> "
        f"{'focused' if ok else 'FAILED'}{': ' + detail if detail else ''} "
        f"({int((time.time() - t0) * 1000)}ms)")
    return ok


# populated last: the adapters must exist before they can be registered
ADAPTERS.update({
    "iterm2": focus_iterm2,
    "tmux": focus_tmux,
})
