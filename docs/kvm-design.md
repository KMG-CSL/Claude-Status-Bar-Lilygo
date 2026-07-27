# KVM: button → focus terminal (design sketch, 2026-07-24)

Goal: long-press a session on the panel → that session's terminal is focused
on the desktop. The display becomes a desk KVM for the Claude fleet.

## The identity chain

1. **session → PID**: the installed hook one-liner runs as a child of the
   claude process, so `$PPID` inside the hook IS the claude PID. The hook
   POST already carries `session_id`; adding `ppid` to the posted JSON gives
   an exact session↔PID binding for free — no cwd guessing, and it resolves
   the N-tabs-in-one-directory ambiguity that defeats CodexBar.
2. **PID → TTY**: `ps -o tty= -p <pid>`.
3. **PID → terminal identity**: the claude process's own environment
   (`ps eww` on macOS, `/proc/<pid>/environ` on Linux) names its host:
   `TERM_PROGRAM=iTerm.app | Apple_Terminal | vscode | ghostty`,
   `TMUX=<socket>,<pid>,<session>`. Detection is therefore **per-session**,
   so mixed setups (iTerm tabs + a VS Code terminal + an ssh'd tmux) all
   work at once with zero configuration.

## Adapter interface

`focus(session_ctx)` where ctx = {pid, tty, cwd, session_id, env}. Builtins:

- **iterm2** — AppleScript: iterate windows/tabs/sessions, match `tty`,
  select + activate. (Proven pattern: ccm's focus.ts.)
- **terminal_app** — same idea, Terminal.app dictionary.
- **tmux** — `tmux list-panes -a -F "#{pane_tty} #{session_name}:#{window_index}"`,
  match tty → `switch-client`/`select-window`. Composes with an outer
  adapter: focus the tmux client's terminal first, then the pane.
- **vscode** — `code -r <cwd>` raises the right window (terminal-pane focus
  is best-effort; document the limit).
- **ghostty** — OSC title tag, best-effort (ccm precedent).
- **linux_generic** — walk the claude PID's parent chain to the terminal
  emulator process, then `wmctrl`/`xdotool` raise-by-PID. Handles the
  separate-windows-at-home case; tabbed Linux terminals are best-effort.
- **exec** — escape hatch: user command template with `{tty} {pid} {cwd}
  {session_id}` substitution.

**Shipped so far** (2026-07-27): the `focus.adapter` key with `auto` /
`none` / a pinned name, honored by both triggers — device long-press
(`input.hold="focus"`) and the desktop app's minimap click. `auto` resolves
to `iterm2` on macOS and `tmux` on Linux; Windows has no adapter, so KVM
reports itself off at startup instead of failing per click. `ADAPTERS` in
`csb/focus.py` is a real name -> function table, and adapters receive the
per-session ctx below (pid, tty, cwd, session_id, env). `exec` and
`overrides` are still unimplemented.

**`auto` resolves to exactly one adapter — there is no fall-through chain.**
A window raise isn't an alternative to a tmux pane select, it's its
companion: in the tmux case both must happen, so a chain would need a
"succeeded but keep going" state. Adapters compose internally instead, via
`raise_window(pid)` — a plain helper, deliberately not an adapter (best
effort, no session identity, its own return). So the `tmux` adapter never
declines: pane select when the session is under tmux, window raise either
way, doing less rather than nothing off tmux. It keeps that name because
tmux is its distinguishing capability. `raise_window` is X11-only —
GNOME/Wayland blocks programmatic activation by design, so it no-ops there.

**Validated against real tmux 3.7b (macOS, 2026-07-27).** The unit tests
stub the runner, so these were checked against a live server instead:

- `list-panes -a -F "#{pane_tty} #{session_name}:#{window_index}.#{pane_index}"`
  emits exactly the format `tmux_pane_for` parses; pane ttys are
  `/dev/ttysNNN` and match `ps -o tty=` after normalization.
- Pane select works, including **across sessions** (`side:0.1` selected
  while `work` was current) — `select-window` handles the session prefix.
- **`switch-client` fails with "no current client" (rc 1) on a detached
  server.** Harmless and deliberately ignored: `select-window` has already
  moved the session, and switch-client only matters when a client is
  attached elsewhere. Don't "fix" this by checking its return.
- The off-tmux and unknown-tty paths were exercised live: no tmux call is
  made at all without `TMUX` in the env, and an unowned tty reports
  `TMUX set but no pane owns …` rather than failing mutely.

Still unproven, and only a Linux box can settle it: `raise_window` itself
(whether `wmctrl -lp` reports the terminal emulator's PID or the shell's),
and everything Wayland.

## Config plane (override-only; auto is the default)

```json
"focus": {
  "adapter": "auto",            // or pin: iterm2|terminal_app|tmux|vscode|ghostty|linux_generic|exec
  "exec": "",                   // used when adapter=exec
  "overrides": [                 // optional per-match pinning
    {"match": {"env.TERM_PROGRAM": "WezTerm"}, "adapter": "exec",
     "exec": "wezterm cli activate-pane --pane-id {wezterm_pane}"}
  ]
}
```

## Trigger + wire

- Device → bridge upstream on the same USB CDC port (Clawdmeter `REFRESH`
  precedent): `{"t":"focus","sl":<slot>}` on long-press of a minimap cell /
  picker tile. Long-press is freed by the session-ring input rework
  (BACKLOG: Input model rework).
- Prereq: **stable slot identity** (BACKLOG, decided) so the pressed letter
  can never mean a different session than the one displayed.

## Failure honesty

Focus is best-effort by nature: report per-adapter capability at install
self-test time ("focus: iTerm2 OK, vscode window-only"), log every focus
attempt with outcome, and never retry-loop a failing adapter (single shot +
status line on the panel: "can't focus E · vscode").
