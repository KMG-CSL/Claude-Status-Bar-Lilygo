# Bridge v2 Design

Decisions, not options. Inputs: structural map of `bridge/claude_bar_bridge.py`, the
things-to-steal specs (items 1–5 + extras), and the 12-factor audit. Firmware is not
edited; the serial contract is frozen except for additive fields.

---

## a) Module split

`claude_bar_bridge.py` stays the file the app imports — it becomes a thin facade that
re-exports the seven public entry points (`data_dir`, `load_config`, `SerialLink`,
`UsageTracker`, `Session` with `.poll(list)`, `find_transcripts`, `build_packet`,
`input_cfg_packet`) plus `main()`. New code lands in a `bridge/csb/` package:

```
bridge/
  claude_bar_bridge.py     # facade: imports from csb.*, keeps main()
  claude_bar_app.py        # unchanged imports; BridgeThread switches to csb.core.BridgeCore
  csb/
    __init__.py
    config.py              # DEFAULT_CONFIG, data_dir, load_config + CSB_* env overrides, log()
    fmt.py                 # parse_ts, pretty_model, pretty_tool, tool_detail, fmt_tokens, fmt_countdown
    session.py             # Session (tailer + parse), noise-event skip list
    engine.py              # StateEngine: unified state derivation (hooks > transcript > mtime)
    hooks.py               # HTTP listener, port file, settings.json installer (opt-in CLI)
    statusline.py          # statusline collector script + capture-file reader (Maciek hardening)
    usage.py               # UsageTracker (OAuth API + estimate), oauth_token()
    limits.py              # rate-limit / error detection from JSONL lines
    serial_link.py         # SerialLink, send_logo
    core.py                # BridgeCore: the ONE loop (rescan/poll/build/send), used by main() and BridgeThread
  tests/                   # stdlib unittest (see §f)
```

The audit's "don't split" advice is overruled *only because* v2 adds three new
subsystems (hooks HTTP, statusline collector, limits) that would push the single file
past 1500 lines — its own stated threshold. The split is mechanical: sections of the
current file move verbatim; the facade keeps every import site working. `oauth_token()`
moves to `usage.py` as a module function; `model_context_limit` imports it from there
(kills the `UsageTracker._oauth_token` cross-class reach; a static alias stays on
`UsageTracker` for compatibility).

Non-goals confirmed from the audit: no dataclass/typing overhaul, no async, no
Transport abstraction, .bat files untouched.

## b) Unified state engine

`csb/engine.py` owns one function:

```python
derive(session, cfg, now) -> StateResult(st, tl, td, el, flags)
```

computed fresh at every packet build (read-time decay — no stored mutable state that
rots; Stargx §6.1). Three evidence tiers, strict precedence:

1. **Hook edges** (Item 1) — authoritative *transitions*. Used only if the session is
   hook-fresh: at least one hook event received for this `session_id` within
   `hook_fresh_s` (default 900). Per-session freshness, not global: one instrumented
   session must not make the engine trust missing edges on an uninstrumented one.
2. **Transcript inference** — the existing JSONL-derived facts (pending tool_use map,
   last_role, trailing "?", timestamps) plus Item 2's write-silence approval flip,
   Item 5's rate-limit detection, the noise-event skip list, and the sticky-done rule.
3. **mtime heuristics** — the current fresh/stale branches (`idle_after_s`,
   `done_after_s`), used when transcript facts are ambiguous.

Hook edges are stored on the session as an edge log: `turn_started_at`
(UserPromptSubmit), `turn_stopped_at` (Stop), `perm_prompt_at` (Notification
permission_prompt / PreToolUse ExitPlanMode|AskUserQuestion), `session_started_at`
(SessionStart — clears inherited wait state, happy's stale-state reset),
`session_ended_at` (SessionEnd).

**`perm_prompt_at` is a latch, not a bare timestamp.** Approving a permission dialog,
accepting an ExitPlanMode plan, or answering AskUserQuestion emits *no*
UserPromptSubmit — the approval arrives as a tool_result / assistant record, never as
a typed prompt — so "newer than the last user prompt" would stay true for the whole
rest of the turn and pin a hook-fresh session on `st=wait` while Claude runs. The
latch is therefore explicitly **cleared** by any of: (a) a PostToolUse edge (the
prompted tool ran — approval happened); (b) any subsequent real transcript activity —
an assistant record, a tool_result, or a new tool_use with a timestamp after
`perm_prompt_at` (noise events excluded); (c) a Stop edge; (d) a UserPromptSubmit
edge; (e) SessionStart. Only an *armed* (uncleared) latch can produce wait in step 2.

When hook-fresh, the working/waiting timers and the
run↔done boundary come from these edges; the transcript still supplies the live
"tool + detail" line (`tl`/`td`) — hooks say *that* a tool ran, the tail says *what*.
Disagreement → the hook edge wins (spec acceptance criterion). No hook traffic ever →
tier 2+3 exactly as today, zero regression.

Derivation order inside `derive()` (first match wins):

1. Rate-limited (Item 5, from `limits.py`) → wait-family, see §e.
2. AskUserQuestion pending, or **armed** `perm_prompt` latch (see clearing rules
   above) → wait.
3. LONG_TOOLS pending (Task/Agent/…) or subagents alive → tool ("delegating"), never
   the approval flip (Item 2 edge case).
4. Non-long pending tool_use:
   - **Hook-fresh:** the Notification(permission_prompt) hook is registered, so the
     *absence* of an armed `perm_prompt` latch is positive evidence the tool was
     auto-approved/allowlisted and is simply executing → **tool**, regardless of
     write silence. The silence heuristic is disabled entirely for hook-fresh
     sessions — hook evidence outranks transcript inference here too, per the tier
     rule ("disagreement → the hook edge wins"). A pre-approved 60 s Bash therefore
     never shows "needs approval".
   - **Not hook-fresh:** write-silence ≥ `approval_silence_s` (default **20**, same
     as today's `wait_tool_s`) → wait (needs approval, Item 2). Below the debounce →
     tool. The debounce stays at today's value because Claude Code writes nothing to
     the main transcript while a tool executes — any `sleep 10`/npm install would
     false-positive at a shorter threshold. Sub-second approval detection comes
     exclusively from the hook edge; uninstrumented sessions keep today's accuracy,
     no worse and no better.
5. Hook-fresh: `turn_stopped_at > turn_started_at` → turn ended; classify the ending:
   if the last assistant text ends with "?" and quiet > `question_after_s` → **wait**
   (Claude asked in prose and awaits an answer — the Stop edge confirms the turn
   ended, it does not negate the question; without this the '?' heuristic would be
   silently lost on every instrumented session), else → done (sticky, Extra B).
   `turn_started_at` newer → run/tool.
6. Transcript tier: trailing "?" + quiet > `question_after_s` → wait; quiet >
   `done_after_s` → done; else run.
7. Stale (mtime ≥ `idle_after_s`): pending → wait, "?" → wait, last=assistant → done,
   else idle.

**Sticky-done (Extra B):** once step 5/6/7 yields done, only a genuine new user prompt
(UserPromptSubmit edge, or a transcript user record that is not a tool_result and not a
noise event) flips it back to run. Late PostToolUse writes, summary records, and noise
events do not. SessionStart reinitializes the sticky flag for a new incarnation.

**Noise skip list (Extra A):** `session.py` ignores record types
`file-history-snapshot`, `queue-operation`, `last-prompt`, `bridge-session` (locally
observed) for all activity/timestamp purposes — they are parsed for nothing and do not
update `last_event_ts`. mtime alone can still be bumped by them, so the engine's
"fresh" test uses `max(last_event_ts, hook edge times)` when available, falling back
to mtime only when the session has produced no real events. Basenames containing
`compact` are skipped at discovery time.

Bug fixes folded in while we're here: `isSidechain` events stay excluded (already
true — keep it under test); abandoned tool_use ids (Escape) are cleared by a Stop edge
or by a subsequent real user message (fixes fragile #5); dead `pending_tool` fields
deleted; `state()` computed once per session per packet — `build_packet` reuses the
`StateResult` for both `act` selection and the packet (fixes fragile #7).

## c) Feature integration

### Item 1 — Hooks-push pipeline (`csb/hooks.py`)

- `ThreadingHTTPServer` on `127.0.0.1`, preferred port `hook_port` (default 45732);
  on `OSError` bind port 0 (auto). Actual port written to
  `cfg["hook_port_file"]` — default **`data_dir()/hook-port`**, always overridable so
  tests point at a temp dir. Nothing under `~/.claude/` is written at runtime: the
  default-on listener writes only into our own data dir, and the port file has no
  consumer until the opt-in installer has run anyway; the installed hook command
  reads the port from the data-dir path (baked into the command at install time).
  Listener thread pushes parsed payloads (`session_id`, `transcript_path`, `cwd`,
  `hook_event_name`, `tool_name`) into a `queue.Queue` drained by
  `BridgeCore.step()`; events route to sessions by `transcript_path` (canonical key)
  with `session_id` as fallback map.
- **Undiscovered sessions — edges are never dropped.** SessionStart/UserPromptSubmit
  fire before the 15 s rescan notices a new transcript (the file may not even exist
  yet). When an event's `transcript_path` matches no known Session, `BridgeCore`
  **eagerly creates** the Session from the payload's `transcript_path` (the tailer
  no-ops until the file has bytes; `cwd` from the payload seeds the project label)
  and triggers an immediate rescan. Without this rule, a buffered-then-dropped
  UserPromptSubmit followed by a post-discovery Stop would leave `turn_stopped_at`
  set with `turn_started_at` unset and step 5 would report done during the first
  live turn — every new session's first ~15 s would lie.
- Installer: `python -m csb.hooks install|uninstall|status [--settings PATH]`
  (default real path only when run interactively by the user — opt-in CLI, never
  automatic, never in tests). Merges into settings.json non-destructively: each
  injected hook entry carries `"_tag": "claudestatusbar"` and the command contains the
  marker name `__claudestatusbar_hook`. Install = remove-tagged-then-add (idempotent);
  uninstall = remove entries where `_tag == "claudestatusbar"` or command contains the
  marker, nothing else; unknown keys and user hooks pass through byte-preserved
  (read → mutate parsed → write with `json.dumps(indent=2)`; a pre-write backup
  `settings.json.csb-bak` is left once).
- Command per event:
  `sh -c 'cat | curl -s -m 2 -X POST http://localhost:PORT/hook -d @- >/dev/null 2>&1 &'`
  — backgrounded, output discarded, Claude never blocks even with the bridge down.
  PORT is read from the port file at hook run time via `$(cat ...)` so bridge restarts
  with a different port keep working.
- **Platform scope: macOS/Linux only in this wave.** The command above is POSIX-only,
  and Windows is otherwise supported (AppData roots, .bat launchers) — installing it
  there would write hooks that fail on every event. The installer refuses on
  `sys.platform == "win32"` with a clear message and non-zero exit, leaving
  settings.json untouched; `status` reports "unsupported platform". The listener
  itself may run (harmless — nothing posts to it). A cmd/PowerShell command variant
  (stdin pipe + port-file read + fire-and-forget) is deferred to a later wave.
- Exactly 8 events registered: SessionStart, SessionEnd, UserPromptSubmit, Stop,
  SubagentStop, PreToolUse (matcher `ExitPlanMode|AskUserQuestion`), PostToolUse,
  Notification (matcher `permission_prompt`). No unofficial names (TaskCompleted /
  SubagentStart are NOT registered).
- SessionStart clears inherited wait/sticky state (happy). If the bridge ever spawns
  its own `claude`, hook events whose `cwd` ends with the dedicated probe suffix are
  dropped (ClaudeBar #172 self-probe guard) — the suffix is a `hooks.py` constant,
  unused until a probe exists.

### Item 2 — Pending tool_use + write-silence → waiting-approval

Lives entirely in `engine.py` step 4, and is **two detectors, not one**:

- **Hook-fresh sessions:** the Notification(permission_prompt) edge is the approval
  detector — sub-second, no debounce, no false positives. Write-silence is *not*
  consulted: a pending tool_use with no armed `perm_prompt` latch means the tool was
  auto-approved and is executing (Claude Code writes nothing to the main transcript
  during Bash/WebFetch execution, so silence is expected, not evidence).
- **Uninstrumented sessions:** newest unmatched non-long tool_use + no transcript
  writes for `approval_silence_s` (default **20** — deliberately equal to today's
  `wait_tool_s`, because at anything shorter every long-running allowlisted tool
  flips the display to "needs approval" and back) → `st=wait`,
  `tl=pretty_tool(name)`, `td=detail` ("needs approval").

Re-evaluated every packet; `send_interval_s` = 1.0 already satisfies the ≤1.0 s
clear requirement, and `BridgeCore` polls a session at 0.5 s cadence while it is
within 1 s of the flip threshold (confirm interval). Task & friends excluded
(step 3); sidechain events already excluded; structured fields only, no prose regex.
`wait_tool_s` remains as the stale-branch fallback knob; the fast path this item
originally promised is delivered by hooks, not by a tighter debounce.

### Item 3 — Statusline pass-through collector (`csb/statusline.py`)

- `python -m csb.statusline install|uninstall [--settings PATH]` wraps the user's
  statusline command as `our-collect | existing-cmd` (or installs collector alone if
  none configured, emitting empty output). Collector = the same module invoked as
  `python -m csb.statusline collect`: reads stdin JSON, writes capture, echoes stdin
  verbatim to stdout — user's statusline output byte-identical; any collector
  exception is swallowed after the echo so it can never break the user's statusline.
- Capture: `data_dir()/statusline/<session_id>.json` containing
  `{ts, session_id, rate_limits, context_used_pct, cost, model, effort}`. Maciek
  hardening: write to `<name>.<pid>.tmp` then `os.replace` (atomic under concurrent
  writers); reads ignore captures older than `statusline_ttl_s` (600); discard any
  `used_percentage > 101` (#52326); treat pct as absent once `now >= resets_at`
  (rollover ghost); a `tombstone: true` capture (plan downgrade) suppresses the bucket.
- Consumption at packet build: fresh capture → `cx` from
  `context_window.used_percentage` (replaces the blocking Models-API call — fixes
  fragile #2 for instrumented sessions), `ef` from `effort.level`, and `us{p5,p7,r5,r7}`
  from `rate_limits` with `est:false`. Capture absent/stale/enterprise-no-rate_limits →
  existing OAuth path unchanged, with the ccburn rollover guard added there too
  (`resets_at < now` → 0%, not a ghost value). Capture arrival timestamp doubles as a
  liveness signal fed to the engine's freshness test. Unknown fields tolerated;
  an example payload is committed as a test fixture.

### Item 4 — Two-timer contract with read-time decay

One crisp rule: **WORKING timer = UserPromptSubmit → Stop; WAITING timer = Stop →
next UserPromptSubmit.** Computed in `derive()` at emit time from stored timestamps:

| st        | el =                                            | source (hook-fresh / fallback)             |
|-----------|--------------------------------------------------|--------------------------------------------|
| run, tool | now − turn_start                                 | UserPromptSubmit edge / last real user msg |
| wait      | now − turn_end                                   | Stop or perm-prompt edge / last event ts   |
| done      | frozen turn_end − turn_start + terminal glyph    | edges / inferred                            |
| idle      | 0                                                | —                                           |

Terminal outcome ships as a new additive field `fin` on ses[]: `"ok"|"fail"|"cancel"`
(empty when not done). Classified at the Stop edge from messages after the last user
timestamp, capped at the last 15 (claude-notifications-go turn-scoped window — kills
ghost ExitPlanMode from old turns): `isApiErrorMessage`/error → fail; Stop with a
still-unmatched tool_use or SessionEnd mid-turn → cancel; else ok. Old firmware
ignores `fin`; a future firmware renders ✓/✗/⨯ beside the frozen time. The
"where every pixel comes from" table above is the BACKLOG timer-audit deliverable;
tests pin every transition boundary so `el` never jumps except at defined edges.
Firmware extrapolation contract unchanged (1 Hz packets keep wait-state ticking).

### Item 5 — Rate-limited state (`csb/limits.py`)

Two regexes over lines `session.py` already parses (structured/synthetic strings, not
agent prose — explicitly endorsed):
`r"limit reached\|(\d+)"` in tool_result text → reset = epoch;
`r"wait\s+(\d+)\s+minutes?"` in system messages → reset = record ts + N·60.
Also: `Claude AI usage limit reached|<epoch>` literal, and
`isApiErrorMessage == true` + `error` (e.g. authentication_failed) → session error
flag. State: engine step 1 while `now < reset`; countdown via `fmt_countdown`
(relative <24 h, absolute beyond — extend fmt_countdown accordingly); `now >= reset`
→ state clears (rollover guard: never show a past-epoch banner). Interacts with the
Item 3 null-after-resets_at rule — both go through one `is_expired(reset, now)`
helper. Alert cooldown: `data_dir()/alerts.json` `{alert_id: {triggered, ts}}`, 24 h
cooldown so a bridge restart doesn't re-fire the device buzzer. Kept incremental-tail
only — no rglob/reparse, no hardcoded plan limits. Wire mapping in §e.

## d) Config surface

`load_config()` (in `csb/config.py`) gains, after the config.json merge, a CSB_*
env-override pass: for every key in the merged dict, `CSB_<KEY.upper()>` if set is
applied via `json.loads` with fallback-to-raw-string (preserves int/float/bool/list
types). `input` sub-keys via `CSB_INPUT_TAP` etc. `data_dir()` honors
`CSB_DATA_DIR`. Unknown keys in user config.json log a warning (typo catch); bad
JSON still warns-and-ignores but now says so with a timestamp. Env-sourced keys are
logged once at startup.

New keys (all with these defaults in DEFAULT_CONFIG and config.example.json):

| key                   | default                                  | feature |
|-----------------------|------------------------------------------|---------|
| `hooks_enabled`       | `true` (listener runs; install stays CLI) | 1 |
| `hook_port`           | `45732` (0 ⇒ pure auto)                  | 1 |
| `hook_port_file`      | `""` ⇒ `data_dir()/hook-port`            | 1 |
| `hook_fresh_s`        | `900`                                    | b |
| `approval_silence_s`  | `20` (uninstrumented sessions only)      | 2 |
| `approval_confirm_s`  | `0.5`                                    | 2 |
| `statusline_ttl_s`    | `600`                                    | 3 |
| `subagent_live_s`     | `90` (was hardcoded)                     | audit |
| `subagent_cache_s`    | `10` (was hardcoded)                     | audit |
| `alert_cooldown_s`    | `86400`                                  | 5 |

`question_after_s` moves to a proper `cfg[...]` read (default already in
DEFAULT_CONFIG — kills the duplicated fallback). `est_cap_*` knobs stay (estimate
path still exists). Logging: module `log(tag, msg)` with ISO timestamp to stdout;
`CSB_DEBUG=1` unmutes the currently-swallowed exceptions (usage API, keychain,
models API, hook listener) — audit M5.

## e) Serial protocol impact

Frozen contract honored: `t=s`, `ses[{pj,nm,md,st,tl,td,ef,tk,sa,el,ti,to,cx,at}]`,
`act`, `us{p5,p7,r5,r7,est}`. No renames/removals. st stays
`run|tool|wait|idle|done` on the wire in v2.

- **RATE-LIMITED on old firmware:** `st=wait`, **`at=false`**, **`tl=""`**,
  `td="rate limit · <countdown>"` (e.g. "rate limit · 2h 14m", ticking at read
  time). Rationale, from the actual firmware paths: with `st=wait` and a non-empty,
  non-"Question" `tl`, row 4 renders `approve: <tl> · <td>` (ino:432-434) — the old
  plan's `tl="RateLimit"` would literally ask the user to *approve a rate limit* for
  hours. `tl=""` skips the `approve:` branch so row 4 shows just the `td` string.
  `at=false` keeps the persistent "! session X waiting" banner (ino:271-280) dark —
  nothing is actionable. Known cosmetic mismatch, accepted: the row-3 headline for
  `st=wait` is hardcoded "Waiting on you" (ino:407); that stays wrong-ish until the
  v2.1 `st=limited` firmware, but it no longer demands approval or lights attention.
- **Auto-follow (`act`) must not be hijacked:** `build_packet`'s waiting-preference
  list (bridge:689-691, `act=waiting[0]`) **excludes** sessions whose wait is
  rate-limit (`lim > 0`) or error-flag driven. Otherwise a limited session pins
  `waiting[0]` for its whole countdown and a genuine approval-wait appearing later
  never becomes `act`, so the firmware never auto-follows to the actionable session.
  Among genuine waits, first_seen order as today; limited/error sessions can still be
  `act` only via the ordinary most-recently-active fallback.
- **Error sessions:** `st=wait`, `tl=""`, `td="error · <short reason>"` (e.g.
  "error · auth failed") — same `tl=""` trick to avoid the `approve:` prefix.
  `at=true` stays: an auth failure *is* actionable by the user. Error sessions are
  still excluded from the `act` waiting-preference (above) so they cannot starve a
  real approval prompt.
- **New additive ses[] fields:** `fin` (`""|"ok"|"fail"|"cancel"`, Item 4),
  `lim` (unix reset epoch, 0 when not limited), `src` (`"h"` hook-fresh /
  `"t"` transcript / `"m"` mtime — debug-visible evidence tier). Old firmware ignores
  unknown keys (ArduinoJson lookup by name).
- **New us field:** none required; `est` already distinguishes API vs estimate, and
  statusline-sourced numbers report `est:false`.
- **Future firmware note (not in this wave):** a v2.1 firmware may add
  `st=limited` (red tile, big countdown) and render `fin` glyphs. Fallback rule for
  that future bridge flag: firmware that doesn't know `limited` must be detectable, so
  the bridge only emits `st=limited` when config `wire_v2: true` is set by a user who
  has flashed the new firmware; default stays the wait-mapping above. Until then no
  new st value ships.

`t=cf` and `t=lg` unchanged.

## f) Test plan

`bridge/tests/`, stdlib unittest, run via
`cd bridge && ./.venv/bin/python -m unittest discover -s tests -v`. No serial ports
ever opened, no network (usage `_try_api` stubbed to None), no real `~/.claude/`
(every installer/port-file test targets `tempfile.TemporaryDirectory()`; `CSB_DATA_DIR`
points captures/alerts at temp).

**Fake clock:** `derive(session, cfg, now)` and `build_packet(..., now=None)` take an
injected `now`; `Session` grows an optional `mtime_fn` (audit M2). Fixtures write
JSONL lines with timestamps relative to a chosen `t0`; tests assert states at exact
thresholds (t0+19.9 s → tool, t0+20.1 s → wait, etc.). No sleeping, no monkeypatching
time.

Layout:

- `helpers.py` — JSONL fixture builder (`user(ts,…)`, `assistant_tool_use(ts,…)`,
  `tool_result(ts,…)`, `noise(ts,type)`), hook-payload builder, temp settings.json
  builder.
- `test_fmt.py` — pretty_model/pretty_tool/tool_detail/fmt_tokens/fmt_countdown
  (incl. new >24 h absolute form)/parse_ts.
- `test_config.py` — env overrides + type coercion, CSB_DATA_DIR, bad JSON, unknown-key
  warning, input deep-merge.
- `test_session.py` — parser facts: token accounting, ctx_tokens, pending_ids
  match/unmatch, sidechain exclusion, rotation/truncation offset reset, noise-event
  skip (a transcript receiving only the 4 noise types bumps nothing), >20 MB seek.
- `test_engine.py` — the state table: every row of §b's derivation order at boundary
  times; approval flip (20 s debounce on uninstrumented sessions, fast tool_result
  never flickers, clear ≤1 s); hook-fresh silence suppression (pending Bash, 60 s of
  silence, no perm_prompt edge → stays tool); perm_prompt latch clearing (armed →
  wait; cleared by PostToolUse / later assistant activity / tool_result answer to
  AskUserQuestion / Stop → never stuck-wait after approval); Stop edge + trailing
  "?" → wait after `question_after_s` (not done); Task exclusion; sticky-done vs
  late PostToolUse/summary writes; SessionStart reset; hook precedence (edge
  contradicts transcript → edge wins); hook-freshness fallback (stale hooks → tier 2
  identical to today's behavior).
- `test_timers.py` — Item 4 el table per state, frozen done duration, `fin`
  classification from the last-15-messages window, no-discontinuity assertions across
  each transition.
- `test_hooks.py` — listener on port 0 + port file correctness after auto-fallback
  (port file in a temp data dir, never `~/.claude/`); POST fixture payloads for all
  8 events parsed and routed by transcript_path; eager Session creation for an
  unknown transcript_path (incl. file-does-not-exist-yet, then first-turn Stop does
  not report done-before-run); installer idempotence (install×2 byte-identical),
  uninstall removes only tagged entries, pre-existing user hooks byte-identical
  through both — all against temp settings.json; installer refuses on a mocked
  `sys.platform == "win32"` leaving settings.json untouched.
- `test_statusline.py` — collector echoes stdin byte-identical (incl. on internal
  error); parallel atomic writes (threads hammering one capture — never a partial
  read); TTL expiry; >101 % drop; resets_at rollover null; tombstone; enterprise
  payload without rate_limits → OAuth fallback; committed example payload fixture.
- `test_limits.py` — both regexes, epoch-in-past suppressed, countdown ticks with
  injected now, alert cooldown persistence across a simulated restart.
- `test_packet.py` — backward-compat tripwire: every firmware-consumed key present
  with correct types (`t,ses[pj,nm,md,st,tl,td,ef,tk,sa,el,ti,to,cx,at],act,us{p5,p7,r5,r7,est}`),
  st ∈ {run,tool,wait,idle,done}, rate-limit wire mapping (`tl=""`, `at=false`,
  countdown in `td`), limited/error sessions excluded from the `act`
  waiting-preference while a genuine approval-wait wins auto-follow, additive fields
  present, `act` and `to_packet` agree (single derive per session).

## g) Wave plan

Small single-purpose commits within each wave; every wave ends green.

- **Wave 1 — refactor + tests (no behavior change).** Package split under `csb/` with
  facade; clock injection (M2); env overrides + CSB_DATA_DIR + log()/CSB_DEBUG
  (M1/M5); extract `BridgeCore` and delete the duplicated loop in
  `claude_bar_app.BridgeThread` (M3); single-derive `build_packet`; hardcoded 90/10 →
  config; tests `test_fmt/config/session/packet` + baseline `test_engine` pinning
  today's behavior. Mac paper cuts (".bat" message, requirements.txt note) ride along.
- **Wave 2 — transcript-only wins.** Item 2 approval flip; Item 5 limits + alert
  cooldown + wire mapping; Extra A noise skip list; Extra B sticky-done; engine
  reordered per §b tiers 2–3; tests `test_engine` (full table), `test_limits`.
- **Wave 3 — hooks (Item 1).** `csb/hooks.py` listener + queue into BridgeCore + port
  file + installer CLI; engine tier 1 + per-session hook-freshness; SessionStart
  reset; abandoned-tool_use clearing via Stop; `test_hooks.py`.
- **Wave 4 — statusline + timers (Items 3, 4).** Collector + hardening + capture
  consumption (context %, effort, us block, OAuth rollover guard); two-timer contract
  exact via edges, `fin` field, timer-audit table verified; `test_statusline.py`,
  `test_timers.py`; retire the Models-API call from the hot path for instrumented
  sessions (kept as last-resort fallback).
