# Claude Status Bar — fleet edition

A ~$25 desk display that shows the live state of your whole Claude Code
fleet at a glance: which sessions are working, which are **waiting on you**,
which are rate-limited or erroring, and how full each context window is.
Multi-session agent state on dedicated hardware is a niche nobody else
occupies — the existing hardware companions are usage gauges or
single-session lights.

This is a hard fork of
[SixSigmaEngineer/Claude-Status-Bar-Lilygo](https://github.com/SixSigmaEngineer/Claude-Status-Bar-Lilygo)
that has grown its own state engine, hooks pipeline, and fleet UI.
See [Relationship to upstream](#relationship-to-upstream).

<!-- TODO: photo of the current fleet-minimap layout. The shot below is
     upstream's original single-session layout and predates the minimap. -->
![Live session status (upstream layout — new photo pending)](docs/images/LilyGo1.png)

## Features (as of 2026-07-24)

- **Fleet minimap** — left column shows one cell per session (up to 8):
  session letter, state color (orange = waiting, green = done, lit = running,
  dim = idle), a context gauge along the bottom of each cell, and the active
  session outlined. A cell blinks white briefly when a session flips to
  waiting.
- **Auto-follow** — the big right-hand panel follows the most recently
  active session, and jumps to any session that is genuinely waiting on you.
- **Waiting / rate-limited / error detection** — pending permission prompts,
  Claude asking a question, `Claude AI usage limit reached` with a live
  reset countdown, and API/auth errors are all distinct wait states.
  Rate-limited sessions never hijack auto-follow.
- **Hooks pipeline (opt-in)** — a localhost HTTP listener in the bridge plus
  eight Claude Code hooks give instant, authoritative state edges
  (turn start/stop, permission prompts) instead of 12–30 s transcript-lag
  heuristics. Uninstrumented sessions fall back to transcript tailing.
- **Statusline collector (opt-in)** — a pass-through wrapper around your
  statusline command captures Claude Code's own `rate_limits`,
  `context_window.used_percentage`, model, and effort per session — no
  extra network calls, byte-identical statusline output.
- **Usage page** — touch-and-hold toggles a 5-hour / 7-day rate-limit page
  (real numbers from the statusline capture or OAuth usage API when logged
  in; local estimate otherwise).
- Touch + button input on both hardware revisions of the board (tap, swipe,
  hold; auto-detected at boot), remappable in config.

## Hardware

| Part | Notes |
|---|---|
| [LilyGo T-Display S3 Long](https://lilygo.cc/products/t-display-s3-long) | ESP32-S3, 3.4" 640×180 LCD, capacitive touch, USB-C. ~$25. |
| USB-C **data** cable | Power and data in one. That's the whole BOM. |

## Quickstart (macOS)

```sh
git clone git@github.com:KMG-CSL/Claude-Status-Bar-Lilygo.git
cd Claude-Status-Bar-Lilygo

# 1. Flash the firmware (self-contained: downloads arduino-cli, the ESP32
#    toolchain (~1.5 GB first run), LilyGo's display driver and libraries
#    into ~/ClaudeBarBuild — nothing system-wide)
firmware/build_and_flash.sh --port /dev/cu.usbmodemXXXX   # omit --port to auto-detect

# 2. Run the desktop app (bridge + live display preview + logo uploader;
#    needs tkinter: brew install python-tk)
bridge/run_app.sh

#    ...or the headless console bridge (only dependency: pyserial,
#    installed into a local venv on first run)
bridge/run_bridge.sh
```

`bridge/run_bridge.sh --demo` sends fake data to verify the pipeline;
`--scan` lists the transcripts found. Windows and Linux paths from upstream
(`build_and_flash.ps1`, `.bat` launchers) still exist but the fork is
developed on macOS/Linux; the hooks/statusline installers below refuse to
run on Windows.

New to this? [docs/ART-SETUP.md](docs/ART-SETUP.md) is the ten-minute
first-boot walkthrough.

## Opt-in installs: hooks and statusline

Both are explicit CLI steps — the bridge never modifies `~/.claude/` on its
own. Both back up your settings once to `~/.claude/settings.json.csb-bak`.

```sh
cd bridge
./.venv/bin/python -m csb.hooks install        # or: uninstall | status
./.venv/bin/python -m csb.statusline install   # or: uninstall | status
```

- **Hooks** merges 8 tagged entries (`"_tag": "claudestatusbar"`) into
  `~/.claude/settings.json`; each is a one-liner that POSTs the hook JSON to
  the bridge's localhost listener, backgrounded so Claude never blocks even
  with the bridge down. `uninstall` removes only our tagged entries.
- **Statusline** wraps your existing statusline command as
  `our-collector | your-command` (output stays byte-identical) and drops
  per-session capture files under the bridge data dir. `uninstall` restores
  your original command exactly.

## Configuration

Copy `bridge/config.example.json` → `bridge/config.json`. Every key can also
be overridden per-run via environment: `CSB_<KEY>` (JSON-parsed, e.g.
`CSB_MAX_SESSIONS=4`, `CSB_PORT=/dev/cu.usbmodem101`), input sub-keys via
`CSB_INPUT_TAP` etc., and `CSB_DATA_DIR` relocates config/captures/port
file. `CSB_DEBUG=1` unmutes normally-swallowed diagnostics.

The most useful knobs:

| Key | Default | Meaning |
|---|---|---|
| `port` | auto | Serial port override (`/dev/cu.usbmodemXXXX`, `COM5`) |
| `max_sessions` | 8 | Sessions shown (firmware cap: 8) |
| `active_window_min` | 30 | Sessions silent longer than this drop off the display |
| `idle_after_s` | 120 | Event-silence before a session reads idle/done |
| `approval_silence_s` | 20 | Pending tool + this much write-silence → "needs approval" (uninstrumented sessions; hooks make this instant) |
| `done_after_s` | 30 | Write-silence after an assistant message → Done |
| `question_after_s` | 12 | ...but a trailing "?" flips to "Waiting on you" this fast |
| `hooks_enabled` | true | Run the localhost hook listener (hook *install* stays an explicit CLI step) |

The full surface (hook ports, statusline TTLs, subagent windows, estimate
caps, input bindings) is documented inline in `bridge/csb/config.py` and
`config.example.json`.

## Relationship to upstream

The original Claude Status Bar is
[SixSigmaEngineer/Claude-Status-Bar-Lilygo](https://github.com/SixSigmaEngineer/Claude-Status-Bar-Lilygo)
— the board bring-up, the transcript-tailing bridge, the Windows tooling,
and the hard-won display/touch quirks documented in
[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) are its work. This fork
diverged around the fleet minimap, the `bridge/csb/` package split, the
hooks/statusline pipelines, and rate-limit/error states. Changes that are
generic rather than fork-specific are offered upstream as PRs once proven
on real hardware (see [BACKLOG.md](BACKLOG.md)).

## License

MIT — see [LICENSE](LICENSE).
