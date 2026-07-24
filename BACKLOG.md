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

## Second display / old-revision touch validation
The AXS15231B (old hw revision) touch path in touch_drv.h is implemented from
LilyGo's example but untested on real hardware. Flash display #2 when it
arrives; auto-detect reports the revision on boot.

## Stale-but-unanswered sessions
Sessions that ended on a question age out of the 30-min display window and
vanish — arguably the ones most worth remembering. Ideas: dim orange count on
the usage page, or extend the window for wait-state sessions only.

## Small polish
- "Done + N subagents" wording (maybe "finishing up") when background agents
  outlive the turn.
- Desktop app preview parity with the redesigned firmware layout (rows drift).
- Waiting-escalation: flash/tint after a session waits > N minutes.
- Swipe direction sanity check when the display is flipped 180°.
- README "what's different in this fork" section for visitors from upstream.
