# Attention board: design notes (2026-10-31)

The problem: 4–5 long-lived Claude Code sessions in parallel, and losing
track of which one needs you. Two views answer it, both fed by the same
engine:

- **The LilyGo** is the *ambient glance*: the minimap cell of any
  session that needs you breathes until you deal with it.
- **The browser board** (`http://127.0.0.1:45732/`) is the *top-down
  view*: what needs me, what finished, where I left off in each session.

## One engine, two consumers

```
hooks ──POST──┐
registry ─────┼─> BridgeCore.step() ── derive() per session ──┬─> serial packet (LCD)
transcripts ──┘        (csb/core.py)     (csb/engine.py)      ├─> fleet.json (status line)
                                                              └─> attention.build_board()
                                                                   └─> BoardFeed ─> SSE /api/events
```

No new process and no second copy of session state. The web routes live
on the hook listener's existing `ThreadingHTTPServer` (`csb/web.py`); the
bridge thread publishes one board document per step, and HTTP threads
only read it. Clicks that need to act (Jump) are queued onto the hook
queue and run on the bridge thread.

## Discovery: the live session registry

Claude Code (observed from 2.1.312) keeps `~/.claude/sessions/<pid>.json` for every
running process: `pid`, `sessionId`, `cwd`, `name` + `nameSource`,
`formerNames`, `status` (`idle`/`busy`), `waitingFor`, `kind`,
`procStart`, `messagingSocketPath`. `csb/registry.py` reads it each step:

- **live set** = entries whose pid is alive *and* whose `procStart`
  matches the process's real start stamp (re-checked every 60 s, to catch
  pid reuse). The stamp is **platform-specific**, and getting it wrong
  drops every session:
  - **Linux**: field 22 of `/proc/<pid>/stat` (starttime, clock ticks
    since boot). Split after the *last* `)` (comm may contain spaces or
    parens), and the field is index 19 of the remainder.
  - **macOS**: `ps -o lstart=`, run with `TZ=UTC` because `procStart` is
    UTC there.
- **current transcript** = `<projects root>/<cwd with non-alnum → '-'>/<sessionId>.jsonl`.
  `sessionId` changes on `/clear`, so a pid's transcript moves; the old
  one drops off the board instead of lingering under a stale name.
- **display name** = the registry `name` when `nameSource == "user"`
  (follows `/rename` instantly), then the transcript's `agent-name`,
  custom title, AI title, first prompt.
- A file caught mid-rewrite reuses its last good parse (no flicker).
- **No registry dir** (older Claude Code, other machines) = fallback mode:
  the old transcript-directory walk plus `active_window_min`, unchanged.

In registry mode a session stays on the board as long as its process is
alive, however long it has been quiet; it ends up in **Parked**
instead of disappearing.

## Evidence tiers (engine.py)

Strict precedence, per session: **hooks** (`src "h"`, while hook-fresh)
→ **registry** (`"r"`) → **transcript** (`"t"`) → **mtime** (`"m"`).
The registry tier is deliberately narrow:

- `waitingFor` non-null → wait (a dialog is up).
- `busy` → overrides a transcript `done`/`idle` (long thinking, slow
  tool) and the write-silence "needs approval" *guess*. It never
  overrides an explicit dialog.
- `idle` is not used to force done: an idle process can still be
  showing a permission prompt that the transcript tier detects better.

## Hooks (10 events)

`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, **`StopFailure`**,
**`SubagentStart`**, `SubagentStop`, `PreToolUse` (ExitPlanMode|AskUserQuestion),
`PostToolUse`, **`Notification`** (no matcher).

- **StopFailure**: a turn that dies on an API error never fires `Stop`;
  without it the tile read "working" forever. Now the turn ends, `fin =
  fail`, and the session waits with the error reason.
- **Notification** routes on `notification_type`: `permission_prompt`,
  `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input` arm
  the wait; `elicitation_complete`/`_response` clear it; `idle_prompt`
  (the ~60 s nag), `auth_success`, `agent_completed`, `quota_*`, plugin
  notifications are ignored.
- **SubagentStart/Stop** track live subagents by `agent_id` (exact,
  where subagent-file mtimes lag; 3 h TTL against a missed Stop).
- `Stop`'s `last_assistant_message` gives the reply text without
  waiting for the transcript tail.

`python -m csb.hooks status` reports **MISSING** events for installs
that predate any of these; re-running `install` adds them.

## Lanes (attention.py), sorted automatically

| Lane | Rule | Sort |
|---|---|---|
| Needs you | actionable wait: permission / plan / question / error | longest waiting first |
| Rate-limited | wait with a known reset | soonest reset first |
| Ready for review | settled + unread | newest finish first |
| Running | run/tool, or done-but-still-delegating | longest running first |
| Done, seen | settled, not unread | most recent activity |
| Parked | quiet ≥ `parked_after_s` (4 h); keeps its unread dot | most recent activity |

**Settled** = done, no live subagents, and no delegated Task-family call
still within a 120 s grace after the turn's Stop. Past the grace, a
delegation whose `tool_result` never came (interrupted, crashed worker)
is retired for good, so it can't pin the session in Running.

**Unread** = settled, not cancelled, and the last reply is newer than
both the user's last *human* prompt and the reply they marked **Seen**
on the board. Transcript user records carry `origin.kind`: `"human"` for
real prompts, `"peer"` for messages from other sessions (with sender
`name`/`body`), others for task notifications. Only human prompts count
as "seen" (older transcripts without `origin`: non-`isMeta` counts as
human). Escape (`fin = cancel`) counts as seen.

**Seen marks** (`csb/marks.py`, `marks.json` in the data dir) store
`{sid: reply_ts}`: the ack names the reply it acknowledged, so a newer
reply is unread again with no expiry logic, and undo is deleting the
entry. The core copies the marks onto each Session every step
(`seen_ts`, `snooze_until`, `note`), and `is_unread` is the one reader,
so the LCD's `ur` and the board's lanes flip together: the cell goes
from solid green to the seen tick with no firmware change. Nothing
auto-acks: expanding a card or jumping to it is looking, not deciding.
The same file holds **snooze** (`{sid: until}`, Needs-you muted for 30
min: out of the headline count, tab title and favicon, sorted last in
its lane, still breathing on the LCD) and the one-line **note** (`#tags`
in it feed the board's tag filter).

## Chime: once per real finish

The chimer sees `attention.chime_state()`: a done-but-delegating parent
reads as `tool`, so an orchestrator with workers chimes once, when the
last worker finishes, not on each interim Stop. A cancelled or
already-seen done reads as `idle` (silent).

## Cards: what's on them, and where it comes from

All cheap: parsed from records the tailer already reads.

- project, display name, model, context %, permission mode, kind
- **current task**: the `in_progress` item of the newest `TodoWrite`
- **You**: last human prompt (600 chars)
- **Claude**: the *end* of the last reply (where it left off)
- subagents (type + age, from hooks), delegated count
- peers: messages to or from **other live registry sessions only**
  (inbound `origin.kind == "peer"` matched by `verifiedPeerPid` or name,
  outbound `SendMessage` `to` matched by `uds:` socket pid or by name with
  any ` [ref]` dropped; never self). Everything else — subagent
  handbacks, `SendMessage` to raw agent ids or `main`, senders that have
  exited — is the session's own worker traffic, shown with its subagents
  (dashed chips). Chips, not drawn links.
- reply: the card shows the reply's **last paragraph**, cut from the top
  (so a closing question is never lost), markdown emphasis stripped;
  expanding the card shows the whole tail.
- Jump: shown only when an adapter can act on *that* session — iTerm2
  when its `TERM_PROGRAM` is `iTerm.app`, tmux when its own environment
  has `TMUX`. Read once per pid from the process env. Never required.
- Layout: lanes side by side at ≥ 1000 px (empty and collapsed lanes
  shrink to a header), stacked on narrow screens.

### Round 2 (2026-11-01): marks, richer cards, filters, Approve

- **Peer chips** carry the peer's `pid` (registry name, else the
  board's own title for that pid, never a socket path). The page
  aggregates per peer: `⇄ C orchestrator · 3 · 4m`, mini slot square
  in the peer's lane colour. Click pins a popover (last three messages,
  Open / Jump to the peer); the peer's card gets an outline. Hover
  state would die on the 1 Hz re-render, so everything is click-to-pin.
  No drawn arrows: lanes re-sort every second.
- **Needs-you cards show the whole pending call** (`pending_tool` +
  `pending`: the full Bash command, the file path, a question's text
  and option labels), from the transcript's `tool_use` input, where the
  LCD's `td` has 36 chars.
- **Drawer** (≥ 1000 px, right side, keyed by the selected card; letters
  select, Esc closes): note editor, the pending call, the full todo list
  with progress (`3/7` on the card's Task row), files the edit tools
  touched *this turn* (relative to cwd, reset on each human prompt), the
  last six exchanges (one entry per reply: the END of its last text),
  the whole last reply, and every peer message with its gist. Sections
  collapse individually (remembered per browser). Below 1000 px the
  card expands in place as before.
- **Filter bar**: projects from `project` (cwd basename), tags from
  `#tags` in notes. Click isolates, shift-click adds, swatch click
  recolours. Filters live in the URL (`?p=a,b&t=x`) so a tab can be
  pinned to one project; never in localStorage, because hidden state in
  an attention tool must be visible. **Guard**: a Needs-you hidden by
  the filter shows as an orange `+1 needs you in <project>` pill in the
  bar, the tab title counts `(shown +hidden)`, and the favicon stays
  fleet-wide. Project colour is a 10 px desaturated swatch beside the
  project name (eight hues, none of them a lane colour, hashed from the
  name, override kept in localStorage); lane colour keeps the slot
  square, borders and state line. `Show` toggles (branch, files, note)
  are display prefs and do live in localStorage.
- **Approve / Deny** on a Needs-you card: `POST /api/answer` types ONE
  key into the session's terminal (`tmux -S <socket> send-keys -t %pane`
  for the pane that owns the session's tty, on the server the session's
  own `$TMUX` names; iTerm2 `write text … newline NO` on the session
  matched by tty). Keys come from `focus.approve_key` / `deny_key`
  (defaults `Enter` / `Escape`: the dialog opens with Yes highlighted;
  whether `1` picks Yes directly is unverified, so the default stays
  Enter). Questions and plans get Jump, not Enter. The rules, after the
  2026-11-01 review:
  - **One predicate** (`attention.answerable`) decides both what the
    card offers (`can_answer`) and what the bridge sends — never
    `need_kind`, which is display text. It requires: `st == wait`, not
    rate-limited, not an error, the hook latch armed with **kind
    `permission`** (the `Notification` type is kept on the Session as
    `perm_kind`; `elicitation_dialog`, `elicitation_url_dialog` and
    `agent_needs_input` arm the wait but are never answerable — Enter
    on an MCP form is not an approval), `src == "h"` (hook-fresh, so a
    stale latch plus a transcript silence guess is refused), the
    pending text not truncated (`PENDING_MAX` 2000 chars; longer is cut
    *and* flagged `pending_truncated`, and the card says so instead of
    offering Approve — never approve text you cannot read), and not
    already answered.
  - **Token-bound**: every Needs-you card carries `answer_token` =
    `sid:latch-ms:newest-pending-tool_use-id`. The POST must echo it;
    the bridge refuses a mismatch ("the dialog changed since the page
    rendered") and records the token as answered *before* sending, so a
    second click on the same dialog, from any tab, types nothing. The
    card shows "accept sent · waiting for the session" until a new
    token (new dialog) replaces it.
  - **Strict binding** (`focus.resolve_strict`): a keystroke needs the
    live registry entry for *this* session id (a `/clear` moves the pid
    to a new id), whose pid is alive, whose start stamp still matches
    the registry's `procStart` (re-read at send time: a reused pid is a
    different program), agreeing with the hook's ppid when known, and
    owning a tty. No cwd scan, no "newest pid" guess — those remain
    Jump's. Without a live registry Approve is simply never offered.
  - **Honest outcome**: `202` means queued. The bridge records
    `answer_result {token, answer, ok, reason}` on the session; it rides
    on the card and the page toasts sent / refused (with the reason) /
    no answer. Adapter failures (`osascript`/`tmux` missing, hung,
    timing out) are contained in `focus._run` and reported as failed —
    they cannot take `step()` down.
  - **Residual**: nothing fires on the approval itself (PreToolUse
    precedes the dialog; PostToolUse follows the tool). If you approve
    a long Bash *in the terminal*, the latch and token stay valid until
    it finishes, and a board click in that window types Enter into a
    running session's input box (harmless when empty; submits whatever
    is typed there). The real fix is a `PermissionRequest` hook
    answering allow/deny (BACKLOG).
- **Keys**: letter selects the card with that slot (so no action is a
  bare letter — `e` used to eat slot E), `Space` seen, `-` snooze,
  `Enter` jump, `Backspace` undo (8 s toast), `Esc` close.

## Security of the localhost endpoints

**Threat model, stated plainly:** the board is a single-user tool on a
single-user machine. Loopback binding plus the Host / Origin /
Content-Type checks below stop *browsers* — a web page you happen to
visit cannot read the board or press Approve. They do **not** stop
other local processes or users: anything running on the box can `POST
/api/answer` directly, and can POST fake hook events that arm the
latch. That is accepted for this setup; it is not multi-tenant safe
and should not be exposed beyond loopback. Transcript text is
sensitive, so:

- GETs require `Host: 127.0.0.1:<port>` or `localhost:<port>`, which
  blocks DNS-rebinding pages from reading `/api/board`.
- `POST /api/focus`, `/api/mark` and `/api/answer` additionally require
  `Content-Type: application/json` (a cross-origin page can't send that
  without a CORS preflight, which we never answer) and a same-host
  `Origin` when present. Bodies are validated field by field (`csb/web.py`
  `_command`): unknown ops, out-of-range snooze minutes, non-string
  notes, a missing answer token, and any number that is a bool, NaN,
  infinite or outside a plausible epoch (`1e9..1e10`; `1e309` parses to
  `inf` and would have reached the page as a bare `Infinity`) are 400,
  never enqueued. The board document is serialised with
  `allow_nan=False` and `marks.json` drops non-finite stamps on load.
  Seen stamps are monotonic per session (a late Seen for an older reply
  can't un-ack a newer one; only an explicit unseen clears) and clamped
  to the newest reply the bridge knows. `/api/answer` is the one route
  that acts on a terminal; its guards are the predicate, token and
  strict binding above.
- Every response carries `Content-Security-Policy: frame-ancestors
  'none'` and `X-Frame-Options: DENY`: the board cannot be framed, so a
  hostile page cannot steer a click onto a real Approve button.
- `board.html` inserts all transcript text via `textContent`, never HTML.
- SSE writes time out after 10 s, so a stalled tab is dropped, not kept.

## Firmware (additive `ur` field)

- `at && !lim` cells breathe (cosine, 2 s, 100→60 %); after 5 min
  waiting they breathe faster and deeper (1.2 s, →40 %). The sharp white
  edge blink still marks the flip and now fires only for actionable waits.
- `ur=1` done = solid green; seen done = idle colours plus a 2 px green
  tick. Old bridges (no `ur`) therefore show every done as seen.
- Redraw goes to the 150 ms cadence only while something pulses.

## Linux notes

- The registry runs on Linux via `/proc/<pid>/stat` (see above); the full
  test suite passes in a Debian container, including the Linux-only
  `/proc` test.
- Ptyxis has no remote-control API and Wayland blocks focus stealing, so
  Jump can't work there. The button now hides itself for sessions not in
  tmux; nothing else depends on it.
- With the board behind a hardware KVM, `/dev/ttyACM0` vanishes when the
  KVM switches away; the serial link already reconnects (EIO / one
  transient EACCES, then reopen) and the next 1 Hz packet carries full state.
