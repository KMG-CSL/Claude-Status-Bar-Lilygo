# Art's ten-minute setup (macOS)

## Coming back to this? Upgrade steps (2026-10-31)

If you set this up in the summer, `git pull` and then do these steps from
the repo root. A fresh setup can skip this section and start at step 0.

What's new: a **browser board** (lanes for *needs you / rate-limited /
ready for review / running / done / parked*, with cards showing your
last prompt, Claude's last reply, subagents and cross-session messages),
exact session discovery from Claude Code's own live session registry, a
durable "unread" flag, and a display that **keeps breathing** on any
session waiting for you. Why and how: [attention-board-design.md](attention-board-design.md).

0. **Stop the bridge/app** if it's running.
1. **Re-install the hooks.** The event set grew from 8 to 10, and
   Notification lost its filter. Without this the board still works,
   with less exact state:
   ```sh
   (cd bridge && ./.venv/bin/python -m csb.hooks install \
              && ./.venv/bin/python -m csb.hooks status)
   ```
   Done when status lists 10 events and prints neither `MISSING:` nor
   `STALE:`. New Claude Code sessions get the new hooks; already-running
   sessions may keep the old set until restarted (the board works either
   way, falling back to the registry and transcripts).
2. **Reflash the display**: `firmware/build_and_flash.sh` (add
   `--port /dev/cu.usbmodemXXXX` if needed), then unplug/replug. This
   adds the breathing animation and unread-vs-seen done cells. A new
   bridge with old firmware still works; you just don't get the new
   animation.
3. **Start one of** `bridge/run_bridge.sh` or `bridge/run_app.sh`, then
   open **http://127.0.0.1:45732/**. It's localhost only, served by the
   bridge itself, with nothing else to install. If that port was taken,
   the bridge picks another and writes it to `bridge/hook-port`, so use
   `http://127.0.0.1:$(cat bridge/hook-port)/`. The board rides on the
   hook listener, so `"hooks_enabled": false` turns it off too.
4. Optional: a terminal **status line** showing who's waiting. Point
   `statusLine.command` in `~/.claude/settings.json` at
   `PYTHONPATH=<repo>/bridge <repo>/bridge/.venv/bin/python -m csb.board line --dir <repo>/bridge/statusline`
   (it also feeds the bridge's context/rate-limit capture, so it replaces
   `csb.statusline install`). Run `python -m csb.board` for the full board
   in a terminal.

Nothing to configure: if your Claude Code writes
`~/.claude/sessions/*.json` (observed from 2.1.312), discovery uses it
automatically; older versions keep the transcript-scan fallback.

---

You have: a LilyGo T-Display-S3-Long, a USB-C **data** cable, a Mac.
Out of the box the board shows LilyGo's factory WiFi demo — ignore it,
step 2 flashes right over it.

## 0. Prerequisites

- **Nothing to brew for flashing.** `firmware/build_and_flash.sh` is
  self-contained: it downloads its own `arduino-cli`, the ESP32 toolchain
  (pinned 2.0.17, ~1.5 GB on first run), LilyGo's display driver, the
  CST3530 touch-firmware blob, and all Arduino libraries into
  `~/ClaudeBarBuild`. Nothing system-wide. It only needs `curl` and
  `python3`, which macOS already has.
- **Headless bridge** (`bridge/run_bridge.sh`): just `python3` — it creates
  its own venv and pip-installs `pyserial` on first run.
- **Desktop app** (`bridge/run_app.sh`, optional but nicer — live preview of
  the display): needs tkinter, which Apple's python3 doesn't ship. Install
  Homebrew Python + Tk: `brew install python3 python-tk`. The app venv
  auto-installs `pillow` itself (only the logo tool needs it).

```sh
git clone git@github.com:KMG-CSL/Claude-Status-Bar-Lilygo.git
cd Claude-Status-Bar-Lilygo
```

## 1. Plug in, find the port

Plug the display in via USB-C. It should appear as `/dev/cu.usbmodem*`:

```sh
ls /dev/cu.usbmodem*
```

Nothing there? Hold the **BOOT** button (side of the board), plug USB in
while holding, release. And make sure the cable does data, not just power.

## 2. Flash

```sh
firmware/build_and_flash.sh            # auto-detects the port
# or: firmware/build_and_flash.sh --port /dev/cu.usbmodemXXXX
```

First run takes a while (the 1.5 GB toolchain download); later runs are a
minute or two. On success it prints `DONE! Unplug and replug the USB cable.`
If upload fails: BOOT-button dance from step 1, run again.

**What the display shows:** on boot, a **solid orange splash** for about a
second (proves the display path works), then the idle screen —
**"Claude Status Bar / connect USB + run bridge on PC"**.

## 3. Run

```sh
bridge/run_bridge.sh --demo    # fake sessions — verifies the whole pipeline
```

**What the display shows:** fake sessions — the left-column fleet minimap
(lettered cells with state colors and context bars) plus the big active
session panel on the right. Ctrl-C, then run it for real:

```sh
bridge/run_app.sh              # desktop app with live preview, or
bridge/run_bridge.sh           # headless console bridge
```

**What the display shows:** your actual Claude Code sessions, or
**"No sessions / waiting for Claude activity..."** until you start one.
Open a terminal, run `claude`, ask it something — a session cell should
appear within a few seconds. Tap to cycle sessions, swipe left/right ditto,
touch-and-hold ~0.6 s for the usage page.

Hold is remappable: set `"input": {"hold": "focus"}` in `bridge/config.json` and a
long-press raises that session's terminal window instead (macOS/iTerm2
only for now) — move the usage page to the BOOT button with
`"input": {"boot_short": "usage"}`. See `bridge/config.example.json`.

## 4. Report your touch revision (please!)

The board shipped in two hardware revisions with different touch chips.
The firmware probes both at boot and prints exactly one of these on serial:

```
[touch] CST3530 detected (new hw revision)
[touch] AXS15231B integrated touch detected (old hw revision)
[touch] no touch controller answered - blind-tap fallback
```

**Tell us which line you get.** The old-revision AXS15231B path is
implemented from LilyGo's example but has never run on real hardware
(BACKLOG: "Second display / old-revision touch validation") — if you have
an old board, you're the first test.

How to see it: the line prints once at boot, so easiest is a serial monitor
(the bridge must not be running — only one process can hold the port):

```sh
~/ClaudeBarBuild/arduino-cli monitor -p /dev/cu.usbmodemXXXX -c baudrate=115200
```

then unplug/replug the display and watch the boot lines. (The running
bridge also echoes device output as `[device] ...` lines, but it usually
reconnects too late to catch the boot banner.)

## Troubleshooting crib

| Symptom | Fix |
|---|---|
| No `/dev/cu.usbmodem*` | Data cable? BOOT-button dance (hold BOOT, replug, release) |
| Flash fails / port busy | Stop the bridge/app first; then BOOT dance, retry |
| Display stuck "Bridge offline" | Bridge not running or wrong port: `bridge/run_bridge.sh --port /dev/cu.usbmodemXXXX` |
| "No sessions" forever | `bridge/run_bridge.sh --scan` lists what transcripts were found |
| Blue/orange colors swapped | Set `SWAP_BYTES 0` in `firmware/claude_statusbar/claude_statusbar.ino`, reflash |
| Display upside down | Hold BOOT ~1 s (flip is saved) |
| Screen totally dark but bridge connects | Backlight issue — see CLAUDE.md symptom table |
| Touch does nothing | Expected on blind-tap fallback (taps only). Report the `[touch]` boot line either way |

Deeper reading: [../README.md](../README.md) for features/config,
[HOW_IT_WORKS.md](HOW_IT_WORKS.md) for architecture and the full hardware
war stories, [../CLAUDE.md](../CLAUDE.md) for the dev crib sheet.
