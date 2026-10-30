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

**Unread** = settled, not cancelled, and the last reply is newer than the
user's last *human* prompt. Transcript user records carry
`origin.kind`: `"human"` for real prompts, `"peer"` for messages from
other sessions (with sender `name`/`body`), others for task notifications.
Only human prompts count as "seen" (older transcripts without `origin`:
non-`isMeta` counts as human). Both timestamps come from the transcript,
so **unread needs no store and survives bridge restarts**. Escape
(`fin = cancel`) counts as seen.

Consequence of "seen = you prompted it": "Done, seen" is rarely
populated. A finished session sits in Review until you reply, or parks
after 4 quiet hours with its dot. A click-to-dismiss is a small
follow-up if that turns out to be wrong (BACKLOG).

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

## Security of the localhost endpoints

The listener binds `127.0.0.1` only. Transcript text is sensitive, so:

- GETs require `Host: 127.0.0.1:<port>` or `localhost:<port>`, which
  blocks DNS-rebinding pages from reading `/api/board`.
- `POST /api/focus` additionally requires `Content-Type:
  application/json` (a cross-origin page can't send that without a CORS
  preflight, which we never answer) and a same-host `Origin` when present.
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
