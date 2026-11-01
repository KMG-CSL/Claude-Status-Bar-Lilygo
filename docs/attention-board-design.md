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
  key into the session's terminal through the focus adapter (`tmux
  send-keys -t <pane>` for the pane that owns the session's tty; iTerm2
  `write text … newline NO` on the session matched by tty). Shown under
  the same rule as Jump, and only for `need == "permission"`. The core
  re-checks on the bridge thread *after* that step's poll: the session
  must still be in a **hook-evidenced** permission wait (`perm_prompt_at`
  armed, `src == "h"`), not a plan, a question, an error, a rate limit,
  nor the transcript tier's silence guess — or nothing is sent, and the
  log says why. Keys come from `focus.approve_key` / `deny_key`
  (defaults `Enter` / `Escape`, Claude Code's dialog accept/cancel).
  Questions get Jump, not Enter.
- **Keys**: letter selects the card with that slot, `e` seen, `s`
  snooze, `Enter` jump, `z` undo (8 s toast), `Esc` close.

## Security of the localhost endpoints

The listener binds `127.0.0.1` only. Transcript text is sensitive, so:

- GETs require `Host: 127.0.0.1:<port>` or `localhost:<port>`, which
  blocks DNS-rebinding pages from reading `/api/board`.
- `POST /api/focus`, `/api/mark` and `/api/answer` additionally require
  `Content-Type: application/json` (a cross-origin page can't send that
  without a CORS preflight, which we never answer) and a same-host
  `Origin` when present. Bodies are validated field by field (`csb/web.py`
  `_command`): unknown ops, out-of-range snooze minutes and non-string
  notes are 400, never enqueued. `/api/answer` is the one route that
  acts on a terminal; its guard is the bridge-side re-check above.
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
