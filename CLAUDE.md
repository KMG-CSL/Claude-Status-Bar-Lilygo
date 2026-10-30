# Claude Status Bar — lab notebook

Desk display for a Claude Code fleet. PC-side Python bridge (`bridge/`,
package `bridge/csb/`) discovers sessions from Claude Code's live registry
(`~/.claude/sessions/<pid>.json`; transcript walk as fallback), tails
transcripts + receives hook events, and feeds one engine to two
consumers: JSON over USB serial to an ESP32-S3 sketch
(`firmware/claude_statusbar/`), and a browser board on the hook listener
(`http://127.0.0.1:45732/`, csb/web.py + csb/board.html + csb/attention.py).
Read docs/attention-board-design.md before changing lanes, unread, the
registry tier, or the web endpoints.

## Commands that work

```sh
firmware/build_and_flash.sh [--port /dev/cu.usbmodemXXXX] [--compile-only]
    # self-contained toolchain in ~/ClaudeBarBuild (esp32 core 2.0.17,
    # LVGL 8.3.11, LilyGo driver auto-copied into the sketch dir)
bridge/run_bridge.sh [--demo|--scan|--no-serial|--replay f.jsonl|--port P]
bridge/run_app.sh                       # desktop app w/ live display preview
cd bridge && ./.venv/bin/python -m unittest discover -s tests -v   # all tests
./.venv/bin/python -m csb.hooks install|uninstall|status           # opt-in
./.venv/bin/python -m csb.statusline install|uninstall|status      # opt-in
./.venv/bin/python -m csb.board [line]   # fleet table / statusLine row (reads fleet.json)
open http://127.0.0.1:45732/             # web board (bridge must be running)
```

Tests never open serial ports, hit the network, or touch `~/.claude/`
(`CSB_DATA_DIR` + `--settings` point everything at temp dirs). Stop the
bridge/app before flashing — the flasher needs the port.

## Serial protocol (115200 baud, newline-delimited JSON, PC → device)

Port discovery: Espressif VID:PID 303A:1001, else lone usbmodem/ttyACM.
Contract is frozen; new fields are additive only (firmware ignores unknowns).

`{"t":"s", "ses":[...], "act":N, "us":{p5,p7,r5,r7,est}}` at 1 Hz, per session:

| field | meaning | | field | meaning |
|---|---|---|---|---|
| pj | project label (≤20) | | el | elapsed s (state-dependent) |
| nm | title (≤56) | | ti/to | tokens in/out |
| md | pretty model | | cx | context % 0-100 |
| st | run\|tool\|wait\|idle\|done | | tk | context tokens, formatted |
| tl | tool name ("" skips `approve:`) | | at | actionable wait (banner) |
| td | detail (≤32) | | **lim** | rate-limit reset epoch, 0=none |
| sa | live subagent count | | **src** | evidence: h(ook)/t(ranscript)/m(time) |
| ef | effort level | | **fin** | turn outcome ok/fail/cancel/"" |
| pm | permission-mode badge | | **ur** | 1 = finished, not seen (unread) |

Also: `{"t":"cf",tap,swipe,hold,bshort,blong}` input bindings on connect;
`{"t":"lg",off,px,last}` logo chunks (48×48 RGB565 LE) + `{"t":"lgclr"}`;
`{"t":"ping"}` keepalive. Device → PC: `[boot] [beat] [touch] [rx]` debug
lines, echoed by the bridge console as `[device] ...`.

Wire mapping for states old firmware doesn't know: rate-limited =
`st=wait, at=false, tl="", td="rate limit · <countdown>"`; error =
`st=wait, at=true, tl="", td="error · <reason>"`. Both are excluded from
the auto-follow waiting preference in `build_packet` (csb/core.py).

## LilyGo T-Display S3 Long touch quirks

Two hardware revisions, auto-detected at boot (`touch_drv.h::touchDetect`),
printed on serial:

- `[touch] CST3530 detected (new hw revision)` — Hynitron CST3530 at I2C
  0x58 (boot mode 0x5A), reset on GPIO2. Chip firmware is verified at init
  and re-uploaded from `cst3530_fw.h` if missing/stale.
- `[touch] AXS15231B integrated touch detected (old hw revision)` — touch
  inside the display controller at I2C 0x3B. **Untested on real hardware**
  (BACKLOG: second-display validation).
- Neither answers → blind-tap fallback (INT pulses = tap only).

The CST3530 emits **phantom down/up pairs right after a lift** — they look
like an extra tap on top of a real swipe. `gestureFrom()` gates on a
lastGesture cooldown: swipe needs >300 ms since the last gesture, tap
>450 ms (and 40–350 ms press, <40 px travel). Don't "fix" jerky swipes by
removing these. First sample after down can be junk (0,0) — originPending
handles it. Controller reports portrait axes: x 0–179, y 0–639; a
landscape-horizontal swipe is a y-run.

Other display facts (full war stories in docs/HOW_IT_WORKS.md): panel is
addressed portrait 180×640, render landscape then transpose; GPIO16 is BOTH
LCD and (old-rev) touch reset; `lcd_PushColors` must be pumped to drain DMA
chunks; SY6970 charger needs reg 0x00=0x3F, 0x09=0x64 when battery-less.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| Blue/orange swapped | RGB565 endianness | `SWAP_BYTES 0` (claude_statusbar.ino:56), reflash |
| Panel dark, boot logs fine | Backlight GPIO1 not driven | `TFT_BL` HIGH after init (setup() does this — check it wasn't lost) |
| Inbound JSON corrupt during redraws | CDC RX buffer overflow | `Serial.setRxBufferSize(16384)` before `begin()` — already set (ino:788) |
| Flash fails "port busy" | Bridge/app holds the port | Stop them first; if still stuck: hold BOOT, replug, release |
| Display stuck "Bridge offline" | Bridge died / wrong port | Restart `bridge/run_bridge.sh`, `--port` to override, `--scan` to sanity-check |
| Tap fires on every swipe | Phantom down/up after lift | Cooldowns above — check they're intact |
| Session stuck "working" after an API error | Hooks predate StopFailure | `python -m csb.hooks status` shows MISSING → re-run `install` |
| Every session missing in registry mode | `procStart` (UTC) compared to local-time `ps` | registry.py runs `ps` with `TZ=UTC`; keep it |
| Status line says "board: bridge off" | No fresh fleet.json (>10 s) | Start the bridge |
| Web board 403 | Opened via a hostname other than 127.0.0.1/localhost | Use `http://127.0.0.1:45732/` (Host check blocks DNS rebinding) |
| Web board won't load on 45732 | Port was taken; listener fell back to an auto port | `cat bridge/hook-port` for the real port (`hooks_enabled: false` also disables the board) |

## Pointers

- docs/HOW_IT_WORKS.md — architecture + all hardware gotchas, in order hit
- docs/bridge-v2-design.md — state engine tiers, hooks/statusline design, wire rules
- docs/attention-board-design.md — web board, lanes, unread, registry tier, hooks, endpoint security
- docs/kvm-design.md — long-press → focus terminal (future)
- BACKLOG.md — priorities; items graduate to upstream PRs when proven
- research/things-to-steal.md — ecosystem survey the v2 design draws from
