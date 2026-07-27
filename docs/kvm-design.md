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
to `iterm2` on macOS and to nothing elsewhere, so KVM reports itself off at
startup instead of failing per click. `exec` and `overrides` below are not
implemented yet; the adapter list is `ADAPTERS` in `csb/focus.py`.

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
