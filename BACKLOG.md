# Backlog

Rough priority order. Items graduate to upstream PRs once proven on real hardware.

## Timer / displayed-value audit (rigor pass)
The elapsed clock can feel decoupled from reality. Trace and document where
every on-screen value comes from, end to end, and verify continuity across
state transitions. Known wrinkles to check:
- `el` changes *meaning* by state (run/tool: time since your last message;
  wait: time since last transcript write; done: last turn's duration) — the
  jump at each transition may be what reads as "decoupled".
- Firmware extrapolates the clock between packets only when animating
  (`el + (millis - lastPacket)`); wait-state ticking relies on 1 Hz packets.
- `turn_start` = last user message that wasn't a tool result; long thinking
  writes nothing, so mid-turn values can plateau then jump.
Deliverable: a "where every pixel comes from" table in HOW_IT_WORKS plus
fixes for any discontinuities found.

## Hooks-based state detection (the real-time upgrade)
Transcript tailing can't see turn boundaries during write-silence (thinking
and an escaped turn look identical). Claude Code hooks fire on the actual
events: `Stop`, `UserPromptSubmit`, `PermissionRequest`, `PostToolUse`.
Plan: tiny HTTP listener in the bridge + opt-in user-scope hook install in
`~/.claude/settings.json` (merge non-destructively), transcript tailer stays
as fallback for uninstrumented sessions. Kills the 12-30s state lag and the
escape blind spot.

## Input model rework: session ring, freed long-press, KVM verb (designed 2026-07-24)
Usage today is hold-to-toggle and traps you: the only exit is another hold,
and while on the usage page tap/swipe still cycle sessions invisibly (`act`
changes, usage keeps rendering — fix regardless). Replace the page toggle
with one navigation ring: `[session A .. N, usage]`, wraparound — usage is
one swipe past either end, and swiping while on usage continues back into
sessions (exit falls out for free).
- **Long-press is freed** → reserve it as the KVM trigger: hold on a session
  = focus that session's terminal. Device sends a line upstream on the same
  USB CDC port (Clawdmeter's device-initiated `REFRESH` precedent); bridge
  maps session → tty → osascript (see ccm steal, things-to-steal §6.2).
- **Double-tap** as alternate KVM trigger: feasible (gesture engine has
  timestamped down/up), but the CST3530's phantom down/up pairs after lift
  (the reason for the 300/450 ms cooldowns in `gestureFrom`) look exactly
  like a double-tap — needs real-hardware tuning, and it costs ~250 ms of
  decision latency on every single tap. Experiment, not default.
- **Config back-compat:** keep `cycle` (sessions only), add `ring`
  (sessions + usage, new swipe default); `hold: usage` stays available for
  people who prefer today's behavior. Reserve verbs `focus`, `picker`, `ack`.
- **Auto-follow vs usage page:** an attention edge (session → wait) pulls
  the display off usage back to status, edge-triggered only — continuous
  would make usage unusable while anything waits.
- Optional: auto-return usage → status after N idle seconds (config, off).

## LHS fleet minimap — replace the logo column (designed 2026-07-24)
The left 178 px (28% of the display) is a logo + context % — the weakest
real estate on the device. Replace with a fleet minimap: one cell per
session (letter, state color, 2-3 px context bar at cell bottom — the
Stargx per-row gauge), active session outlined, layout adapting from 2 big
cells to a 2x4 grid. Logo demotes to boot splash / "No sessions" screen;
numeric context joins row 5 on the right.
- **Physical constraint that shapes everything:** the square is ~23x23 mm of
  glass; 8 cells = ~11.6x5.2 mm — fine to look at, too small to tap
  individually. So two levels: minimap is ambient (whole-zone tap = open a
  full-screen session picker, 8 tiles of ~19x11 mm, tap to jump, auto-
  dismiss). The picker's hit-test plumbing is the future KVM entry point.
- **Motion restraint (survey attention-grammar):** static colors only; at
  most a one-shot flash on a state-*worsening* edge. Auto-follow already
  yanks the big side to the waiting session — that layout change is itself
  the peripheral-vision alert; sustained pulsing is clutter/alert-fatigue.
- **Open question — vet by experiment, not argument:** per-session grid
  (identity, but letters shift as sessions come/go) vs aggregate rollup
  (proportional color bands / "2▶ 1! 3✓" counts — faster read, no identity).
  Plan: hotkey-switchable A/B variants in the desktop preview app
  (claude_bar_app.py renders from the same packets), run against the live
  fleet for a day, port the winner to firmware. Touch facts: CST3530 gives
  real per-pixel coords (tap zone hit-test is arithmetic on tXl/tYl we
  already track); old-revision AXS boards are blind-tap — zone features
  need a degrade path. Left column is a contiguous portrait band (rows
  0-178), so partial pushes could hit ~20 fps there if ever needed.

**2026-07-24 live-trial results (grid shipped to firmware same day):**
- Desktop A/B verdict: control layout decisively rejected; grid and rollup
  both strong, grid chosen and ported to the panel. Context gauge needs the
  black inset track (colored bar directly on a green/orange cell fill is
  unreadable; short-bar vs no-gauge ambiguous). Done in firmware.
- **Decided — stable slot identity (KVM prerequisite):** a session keeps
  its letter for its lifetime; freed letters are reused last. Assign slots
  in the bridge; firmware renders what it's told. Observed live: hiring
  session drifted B→A between packets — "long-press B" must never mean
  someone else.
- **Decided — capacity by curation, not smaller cells:** quadrant-size
  cells are the floor. For >4 sessions: waiting/running always get cells,
  idle/done collapse first (Stargx idle-dedup), "+N idle" overflow chip;
  2x3 grid (~46px cells) only when >4 genuinely active. Wants to track
  fleets of 5-8.
- Field notes: cwd basename is weak identity (3 tabs all labeled "Brain";
  custom rename fixed it — title fallback chain steal validated). Process
  scan found all 5 live PIDs but 3 shared cwd ~/Brain — PID↔session
  binding ambiguous without hooks (CodexBar's one-PID-per-cwd limit seen
  live). Remote Control `bridge-session` + `last-prompt` tail records
  observed in a stale transcript — Stargx noise-event skip list validated.
- Flash-on-wait-edge: shipped in firmware (1.6s white blink), unjudged —
  user hasn't caught one live yet.

## KVM: make adapters real, then add Linux/tmux (next up, specced 2026-07-27)
`focus.adapter=auto` resolves to nothing off macOS, so KVM is off on the
Ubuntu box. **But the adapter seam is currently fake, and that's the actual
work.** Three defects, in dependency order — 1 unblocks 2 unblocks 3:

1. **The resolved adapter name is thrown away.** `adapter_for()` returns a
   name, `enabled()` checks it's non-empty, and then nothing reads it:
   `focus_slot` ends by calling `focus_tty()`, hardcoded to osascript. So
   "add an entry to `ADAPTERS`" is *not* true — adding tmux today means
   editing the tail of the shared function both platforms run. Fix: make
   `ADAPTERS` a dispatch table, name -> function
   (`{"iterm2": focus_iterm2, "tmux": focus_tmux, ...}`), and have
   `focus_slot` call the resolved one. Then an adapter really is one entry.

2. **Adapters get only the tty; they need the claude PID too.** tty is
   sufficient for iTerm2 because iTerm2 owns every tab. On Linux it isn't:
   a session may be in tmux, or a bare TTY/X shell, and the adapter has to
   know which. `/proc/<pid>/environ` (`TMUX=...`) answers it — the exact
   per-session detection kvm-design.md specifies. The PID is already sitting
   in `focus_slot`; it just isn't passed down. Without it `auto` has to
   guess, and it guesses wrong for every non-tmux session.

**Explicitly rejected (2026-07-27): a decline-and-fall-through adapter
chain.** Tempting after 2, and the wrong shape. A window raise is not an
*alternative* to a tmux pane select, it's its companion — in the tmux case
*both* must happen. A chain would either stop once tmux succeeded, or need
a "succeeded but keep going" state, which is worse than what we have now.
So: `adapter_for()` keeps returning exactly one string, the startup line
stays a truthful static statement, pinning stays a pin, and `auto`-by-
platform simply gains linux -> tmux. No new return states, no chain
machinery.

Composition happens *inside* the adapter instead. `raise_window(pid)` is a
plain helper, deliberately **not** an adapter: different contract (best
effort, no session identity, different return), so there is no adapter
calling an adapter — just one adapter calling a shared utility, which is
ordinary code.

The tmux adapter therefore **never declines**: it selects the pane when the
session is under tmux (`tmux list-panes -a -F "#{pane_tty} ..."` matched on
tty, then `switch-client`/`select-window`) and raises the window either
way. Off tmux it does *less*, not nothing. It keeps the name `tmux` because
that's its distinguishing capability and it matches kvm-design.md's
vocabulary — the degrade-to-raise-only behavior belongs in its docstring,
not in its name.

## Second display / old-revision touch validation
The AXS15231B (old hw revision) touch path in touch_drv.h is implemented from
LilyGo's example but untested on real hardware. Flash display #2 when it
arrives; auto-detect reports the revision on boot.

## Stale-but-unanswered sessions
Sessions that ended on a question age out of the 30-min display window and
vanish — arguably the ones most worth remembering. Ideas: dim orange count on
the usage page, or extend the window for wait-state sessions only.

## "Done + N subagents" — bug or feature? (decide after ecosystem survey)
Observed live: main session shows Done while 6 workflow subagents run in the
background. Technically true (the turn ended, the user CAN reply) but "Done"
undersells that work is still in flight. Candidate treatments: a distinct
state word ("Delegating" / "N agents working"), keep Done but make the
subagent count more prominent, or promote it to a distinct color. Deferred
until we see how other projects (octomux kanban states, dashboards from the
overnight survey) model "idle orchestrator, busy workers".

## Small polish
- Desktop app preview parity with the redesigned firmware layout (rows drift).
- Waiting-escalation: flash/tint after a session waits > N minutes.
- Swipe direction sanity check when the display is flipped 180°.
- README "what's different in this fork" section for visitors from upstream.
