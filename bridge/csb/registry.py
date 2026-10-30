"""Claude Code's live session registry: ~/.claude/sessions/<pid>.json.

Every running Claude Code process (>= ~2.1.2xx) keeps one small JSON file
here — pid, sessionId, cwd, user-set name, coarse status (idle/busy),
kind, messaging socket — and rewrites it as those change. That gives
exact, hook-free discovery: the set of live sessions is the set of
entries whose pid is alive, and the CURRENT transcript of each (sessionId
changes on /clear) is <projects root>/<sanitized cwd>/<sessionId>.jsonl.
Transcript-directory walking stays as the fallback for machines/versions
without a registry.

A pid can be reused after a crash left a stale file behind; procStart
(the process start stamp, platform-specific: see _proc_start) is
re-checked every REVALIDATE_S.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time

from .config import CLAUDE_DIR, debug

_SANITIZE = re.compile(r"[^A-Za-z0-9]")
REVALIDATE_S = 60


def project_dir_name(cwd):
    """The projects/ directory name Claude Code derives from a cwd."""
    return _SANITIZE.sub("-", cwd or "")


def registry_dir(cfg=None):
    return (cfg or {}).get("registry_dir") or os.path.join(CLAUDE_DIR,
                                                           "sessions")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def stat_starttime(stat_text):
    """Field 22 (starttime, clock ticks since boot) of a /proc/<pid>/stat
    line, as a string; "" if unparseable. comm (field 2) is parenthesised
    and may itself contain spaces or ")", so split after the LAST ")":
    the remainder starts at field 3, which puts field 22 at index 19."""
    rest = stat_text[stat_text.rfind(")") + 1:].split()
    return rest[19] if len(rest) > 19 else ""


def _proc_start(pid):
    """This process's start stamp in the form Claude Code writes to
    procStart for this platform ("" if unknown):

      linux   /proc/<pid>/stat starttime (clock ticks since boot)
      darwin  `ps -o lstart=`, printed in UTC (procStart is UTC there;
              local time never matches)
    """
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8",
                      errors="replace") as f:
                return stat_starttime(f.read())
        except OSError:
            return ""
    if sys.platform == "win32":
        return ""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC"))
        return " ".join(out.stdout.split())
    except Exception:
        return ""


class Registry:
    """Reads the registry each step (a handful of small files); caches
    the procStart verdict per (pid, procStart) so ps runs once per
    process, not once per second."""

    def __init__(self, cfg=None, alive=_pid_alive, lstart=_proc_start):
        self.dir = registry_dir(cfg)
        self._alive = alive
        self._lstart = lstart
        self._verified = {}          # (pid, procStart) -> (bool, checked ts)
        self._last_good = {}         # file path -> last parsed entry

    def available(self):
        return os.path.isdir(self.dir)

    def read(self):
        """-> {sessionId: entry} for live processes, or None when this
        machine has no registry (fallback: transcript discovery)."""
        if not self.available():
            return None
        live = {}
        paths = glob.glob(os.path.join(self.dir, "*.json"))
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    e = json.load(f)
                if not isinstance(e, dict):
                    raise ValueError("not an object")
                self._last_good[path] = e
            except Exception:
                # caught mid-rewrite: reuse the last good parse (still
                # liveness-checked below) instead of blinking it off
                e = self._last_good.get(path)
                if e is None:
                    continue
            pid, sid = e.get("pid"), e.get("sessionId")
            if not isinstance(pid, int) or not sid or not self._live(e):
                continue
            prev = live.get(sid)
            if prev is None or (e.get("updatedAt") or 0) > \
                    (prev.get("updatedAt") or 0):
                live[sid] = e
        for path in set(self._last_good) - set(paths):
            del self._last_good[path]          # file removed: forget it
        return live

    def _live(self, e, now=None):
        pid = e["pid"]
        if not self._alive(pid):
            self._verified = {k: v for k, v in self._verified.items()
                              if k[0] != pid}
            return False
        want = " ".join(str(e.get("procStart") or "").split())
        if not want:
            return True
        now = time.time() if now is None else now
        key = (pid, want)
        ok, checked = self._verified.get(key, (None, 0.0))
        # re-check periodically: the process can die and its pid be reused
        # between polls, which kill(pid, 0) alone cannot see
        if ok is None or now - checked > REVALIDATE_S:
            got = self._lstart(pid)
            # unknown (ps failed) is trusted: a false "dead" hides a real
            # session, a false "alive" only shows a stale one
            ok = (not got) or got == want
            self._verified[key] = (ok, now)
            if not ok:
                debug("registry", f"pid {pid} reused (start {got!r} != "
                                  f"{want!r}); stale entry ignored")
        return ok


def transcript_path(entry, roots):
    """-> the entry's current transcript path. The sanitized-cwd guess is
    exact for every session observed; a glob over the projects roots is
    the fallback when the cwd was moved or sanitizing ever changes."""
    sid, cwd = entry.get("sessionId", ""), entry.get("cwd") or ""
    if isinstance(roots, str):
        roots = [roots]
    for root in roots:
        p = os.path.join(root, _SANITIZE.sub("-", cwd), sid + ".jsonl")
        if os.path.exists(p):
            return p
    for root in roots:
        hits = glob.glob(os.path.join(root, "*", sid + ".jsonl"))
        if hits:
            return hits[0]
    # not written yet (brand-new session): the sanitized path is where it
    # will appear; Session.poll no-ops until it exists
    base = roots[0] if roots else os.path.join(CLAUDE_DIR, "projects")
    return os.path.join(base, _SANITIZE.sub("-", cwd), sid + ".jsonl")
