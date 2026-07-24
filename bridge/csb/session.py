"""Session: incremental JSONL transcript tailer + parsed facts, and
transcript discovery."""

import json
import os
import time

from . import engine
from .fmt import fmt_tokens, parse_ts, pretty_model, pretty_tool, tool_detail
from .usage import model_context_limit


def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


class Session:
    def __init__(self, path, mtime_fn=None):
        self.path = path
        self._mtime_fn = mtime_fn or safe_mtime
        self.offset = 0
        self.name = ""
        self.project = ""             # basename of the session's cwd
        self.ai_title = ""            # Claude Code's generated session title
        self.custom_title = ""        # user-set title (wins over ai_title)
        self.model = ""
        self._sa_count = 0            # active subagent transcripts
        self._sa_checked = 0
        self.tok_in = 0
        self.tok_out = 0
        self.ctx_tokens = 0
        self.effort = ""
        self.turn_start = None        # ts of last real user message
        self.last_event_ts = None
        self.last_role = ""           # user / assistant
        self.last_assistant_text = ""
        self.pending_tool = ""        # tool name awaiting result
        self.pending_tool_ts = None
        self.pending_ids = {}         # tool_use id -> (name, ts)
        self.first_seen = time.time()

    # ---- incremental parse ----
    def poll(self, usage_events):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:          # truncated/rotated
            self.offset = 0
        if size == self.offset:
            return
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                if self.offset == 0 and size > 20 * 1024 * 1024:
                    f.seek(size - 5 * 1024 * 1024)
                    f.readline()  # skip partial line
                else:
                    f.seek(self.offset)
                for line in f:
                    self._line(line, usage_events)
                self.offset = f.tell()
        except OSError:
            pass

    def _line(self, line, usage_events):
        line = line.strip()
        if not line or line[0] != "{":
            return
        try:
            rec = json.loads(line)
        except Exception:
            return
        rtype = rec.get("type", "")
        ts = parse_ts(rec.get("timestamp")) or time.time()

        if rtype == "summary":
            s = rec.get("summary", "")
            if s:
                self.name = s[:24]
            return

        if rtype == "ai-title":
            t = rec.get("aiTitle") or rec.get("title") or ""
            if t:
                self.ai_title = t.strip()
            return

        if rtype == "custom-title":
            t = rec.get("customTitle") or rec.get("title") or ""
            if t:
                self.custom_title = t.strip()
            return

        cwd = rec.get("cwd")
        if cwd and not self.project:
            self.project = os.path.basename(cwd.rstrip("/\\")) or cwd

        if rec.get("isSidechain"):
            return

        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content = content or []

        if rtype == "user":
            has_tool_result = False
            text = ""
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_result":
                        has_tool_result = True
                        self.pending_ids.pop(b.get("tool_use_id", ""), None)
                    elif b.get("type") == "text":
                        text += b.get("text", "")
            if not has_tool_result:
                self.turn_start = ts
                if not self.name and text:
                    self.name = text.strip().replace("\n", " ")[:24]
            self.last_role = "user"
            self.last_event_ts = ts

        elif rtype == "assistant":
            m = msg.get("model") or ""
            if m and not m.startswith("<"):   # skip "<synthetic>" system records
                self.model = m
            usage = msg.get("usage") or {}
            i = usage.get("input_tokens", 0) or 0
            o = usage.get("output_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cc = usage.get("cache_creation_input_tokens", 0) or 0
            if i or o or cr or cc:
                self.tok_in += i + cc
                self.tok_out += o
                self.ctx_tokens = i + cr + cc
                usage_events.append((ts, i + cc + o))
            for key in ("effort", "reasoningEffort", "thinkingEffort"):
                if rec.get(key):
                    self.effort = str(rec[key])[:10]
            text = ""
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    self.pending_ids[b.get("id", "")] = (
                        b.get("name", "Tool"), ts, tool_detail(b.get("input")))
                elif b.get("type") == "text":
                    text += b.get("text", "")
            if text:
                self.last_assistant_text = text[-400:]
            self.last_role = "assistant"
            self.last_event_ts = ts

    # ---- derived state ----
    def mtime(self):
        return self._mtime_fn(self.path)

    def state(self, cfg, now=None):
        """-> (state, tool_name, detail). Kept for compatibility;
        the derivation itself lives in csb.engine.derive()."""
        r = engine.derive(self, cfg, now)
        return r.st, r.tl, r.td

    def subagent_count(self, cfg=None, now=None):
        """Active helper-agent transcripts under <slug>/<session-uuid>/subagents."""
        if now is None:
            now = time.time()
        cfg = cfg or {}
        if now - self._sa_checked < cfg.get("subagent_cache_s", 10):
            return self._sa_count
        self._sa_checked = now
        n = 0
        base = os.path.join(os.path.dirname(self.path),
                            os.path.splitext(os.path.basename(self.path))[0],
                            "subagents")
        if os.path.isdir(base):
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    if fn.startswith("agent-") and fn.endswith(".jsonl"):
                        if now - safe_mtime(os.path.join(dirpath, fn)) < \
                                cfg.get("subagent_live_s", 90):
                            n += 1
        self._sa_count = n
        return n

    def to_packet(self, cfg, now=None, state=None):
        """Wire dict for ses[]. `state` takes a precomputed StateResult so
        build_packet derives each session exactly once per packet."""
        if now is None:
            now = time.time()
        if state is None:
            state = engine.derive(self, cfg, now)
        st, tool, detail, el = state
        # context window: per-model via the Models API, config as fallback;
        # if we've measured more tokens than the limit, it's clearly bigger
        limit = model_context_limit(self.model, cfg["context_limit"])
        if self.ctx_tokens > limit:
            limit = 1000000
        ctx = int(round(100.0 * self.ctx_tokens / limit))
        title = (self.custom_title or self.ai_title or self.name
                 or os.path.basename(os.path.dirname(self.path))[:24])
        return {
            "pj": self.project[:20],
            "nm": title[:56],
            "md": pretty_model(self.model),
            "st": st,
            "tl": pretty_tool(tool),
            "td": detail[:32],
            "sa": self.subagent_count(cfg, now),
            "ef": self.effort,
            "el": el,
            "ti": self.tok_in,
            "to": self.tok_out,
            "cx": min(ctx, 100),
            "tk": fmt_tokens(self.ctx_tokens),
            "at": st == "wait",
        }


def find_transcripts(roots):
    # os.walk (not glob) — transcripts live inside hidden ".claude" folders,
    # which glob's ** refuses to enter. Skip audit logs (encrypted, not
    # transcripts).
    if isinstance(roots, str):
        # Guard for direct callers: iterating a bare string yields characters,
        # and os.path.isdir("/") is True — that walks the entire filesystem.
        roots = [roots]
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            if "subagents" in dirs:
                dirs.remove("subagents")   # helper agents aren't top-level sessions
            for fn in files:
                if fn.endswith(".jsonl") and fn != "audit.jsonl":
                    path = os.path.join(dirpath, fn)
                    try:
                        found[path] = os.path.getmtime(path)
                    except OSError:
                        continue
    return found
