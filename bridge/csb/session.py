"""Session: incremental JSONL transcript tailer + parsed facts, and
transcript discovery."""

import json
import os
import time

from . import engine, limits
from .fmt import fmt_tokens, parse_ts, pretty_model, pretty_tool, tool_detail
from .usage import model_context_limit


def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


# Bookkeeping records Claude Code appends without any real activity behind
# them (Extra A, Stargx §6.1 + locally observed). They must never count as
# activity: parsed for nothing, and they don't update last_event_ts — a
# transcript receiving only these stays idle even though mtime is bumped.
NOISE_TYPES = frozenset((
    "file-history-snapshot",
    "queue-operation",
    "last-prompt",
    "attachment",
    "bridge-session",
))


def _result_text(block):
    """Text of a tool_result block; content is a str or a block list."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


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
        self.limit_reset = 0.0        # unix epoch the rate limit lifts, 0 = none
        self.error = ""               # short API-error reason ("" = none)
        self.done_latch = None  # sticky-done: (turn_start, frozen_el, armed_event_ts)
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
        if rtype in NOISE_TYPES:
            return
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

        if rtype == "system":
            # "please wait N minutes" style throttle notices (Item 5)
            txt = rec.get("content")
            if not isinstance(txt, str):
                txt = "".join(b.get("text", "") for b in content
                              if isinstance(b, dict) and b.get("type") == "text")
            reset = limits.scan_text(txt, ts, relative=True)
            if reset:
                self.limit_reset = max(self.limit_reset, float(reset))
            return

        if rtype == "user":
            has_tool_result = False
            text = ""
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "tool_result":
                        has_tool_result = True
                        self.pending_ids.pop(b.get("tool_use_id", ""), None)
                        # structured "limit reached|<epoch>" only: tool
                        # output is agent-visible prose, and "wait N
                        # minutes" in a build log is not a rate limit
                        reset = limits.scan_text(_result_text(b), ts)
                        if reset:
                            self.limit_reset = max(self.limit_reset,
                                                   float(reset))
                    elif b.get("type") == "text":
                        text += b.get("text", "")
            if not has_tool_result:
                self.turn_start = ts
                self.error = ""       # a real prompt is the user acting on it
                if not self.name and text:
                    self.name = text.strip().replace("\n", " ")[:24]
            self.last_role = "user"
            self.last_event_ts = ts

        elif rtype == "assistant":
            if rec.get("isApiErrorMessage"):
                # synthetic record: a usage-limit banner or an API error
                # (claude-notifications-go analyzer.go:198). It is real
                # activity, but its text must not feed the "?" heuristics.
                txt = "".join(b.get("text", "") for b in content
                              if isinstance(b, dict) and b.get("type") == "text")
                # synthetic (never agent prose): the relative form is safe
                reset = limits.scan_text(txt, ts, relative=True)
                if reset:
                    self.limit_reset = max(self.limit_reset, float(reset))
                else:
                    self.error = limits.short_reason(rec.get("error") or txt)
                self.last_role = "assistant"
                self.last_event_ts = ts
                return
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
                # a successful API call proves limit/error are over
                self.limit_reset = 0.0
                self.error = ""
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
        st, tool, detail, el = state.st, state.tl, state.td, state.el
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
            # a rate-limited wait is not actionable: at stays False so the
            # firmware's "! session waiting" banner stays dark (§e). An
            # API error IS actionable, so it keeps at=True.
            "at": st == "wait" and not state.lim,
            "lim": int(state.lim),      # additive field; old firmware ignores
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
                # "compact" basenames are compaction artifacts, not sessions
                if fn.endswith(".jsonl") and fn != "audit.jsonl" \
                        and "compact" not in fn:
                    path = os.path.join(dirpath, fn)
                    try:
                        found[path] = os.path.getmtime(path)
                    except OSError:
                        continue
    return found
