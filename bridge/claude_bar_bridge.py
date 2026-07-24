#!/usr/bin/env python3
"""
Claude Status Bar bridge — PC side.

Tails Claude session transcripts (Claude Desktop / Cowork and Claude Code)
and streams live status to a LilyGo T-Display S3 Long over USB serial as
newline-delimited JSON.

This file is a thin facade over the `csb` package: it re-exports the public
entry points (used by claude_bar_app.py and scripts) and keeps the CLI.

Usage:
    python claude_bar_bridge.py              # normal operation
    python claude_bar_bridge.py --scan       # show which transcript files were found
    python claude_bar_bridge.py --demo       # fake data, test the display without Claude
    python claude_bar_bridge.py --no-serial  # print packets to console instead of serial
    python claude_bar_bridge.py --port COM5  # force a serial port

Requires: pip install pyserial
"""

import argparse
import json
import os
import sys
import time

from csb.config import (
    APPDATA, CLAUDE_DIR, DEFAULT_CONFIG, HOME, IS_MAC, IS_WINDOWS,
    LOCALAPPDATA, data_dir, debug, default_roots, input_cfg_packet,
    load_config, log,
)
from csb.fmt import (
    fmt_countdown, fmt_tokens, parse_ts, pretty_model, pretty_tool, tool_detail,
)
from csb.core import BridgeCore, build_packet
from csb.engine import LONG_TOOLS, StateResult, derive
from csb.session import Session, find_transcripts, safe_mtime
from csb.usage import UsageTracker, model_context_limit, oauth_token
from csb.serial_link import SerialLink, send_logo


def demo_packets():
    import itertools
    states = itertools.cycle([
        ("run", "", 6), ("tool", "Read", 6), ("tool", "Bash", 7),
        ("run", "", 8), ("wait", "Edit", 9), ("done", "", 9),
    ])
    t0 = time.time()
    while True:
        st, tl, cx = next(states)
        yield {
            "t": "s",
            "ses": [{"pj": "DemoProject", "nm": "Fix the flux capacitor",
                     "md": "Fable 5", "st": st, "tl": tl,
                     "td": "npm test" if tl else "", "sa": 2,
                     "ef": "medium", "el": int(time.time() - t0),
                     "ti": 56500, "to": 4700, "cx": cx, "tk": "12k",
                     "at": st == "wait"},
                    {"pj": "SecondProj", "nm": "Refactor auth flow",
                     "md": "Opus 4.8", "st": "run", "tl": "", "td": "",
                     "sa": 0, "ef": "xhigh", "el": 64, "ti": 37700,
                     "to": 3200, "cx": 55, "tk": "110k", "at": False}],
            "act": 0,
            "us": {"p5": 11, "p7": 34, "r5": "36m", "r7": "5d10h", "est": True},
        }


def main():
    ap = argparse.ArgumentParser(description="Claude Status Bar bridge")
    ap.add_argument("--scan", action="store_true", help="list transcript files found and exit")
    ap.add_argument("--demo", action="store_true", help="send fake data to test the display")
    ap.add_argument("--no-serial", action="store_true", help="print packets instead of sending")
    ap.add_argument("--port", default="", help="serial port override, e.g. COM5")
    ap.add_argument("--replay", default="", help="parse a single .jsonl file and print state")
    args = ap.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port

    if args.scan:
        found = find_transcripts(cfg["roots"])
        if not found:
            print("No transcript files found. Checked roots:")
            for r in cfg["roots"]:
                print(f"  {r}  {'(exists)' if os.path.isdir(r) else '(missing)'}")
            return
        for path, mtime in sorted(found.items(), key=lambda kv: -kv[1])[:25]:
            age = (time.time() - mtime) / 60
            print(f"  {age:7.1f} min ago  {path}")
        print(f"\n{len(found)} transcript file(s) total.")
        return

    if args.replay:
        s = Session(args.replay)
        s.poll([])
        print(json.dumps(s.to_packet(cfg), indent=2))
        return

    link = None if args.no_serial else SerialLink(cfg["port"], cfg["baud"])
    last_ser = None   # send the logo once per (re)connect
    print("Claude Status Bar bridge running. Ctrl+C to stop.")

    if args.demo:
        for pkt in demo_packets():
            line = json.dumps(pkt, separators=(",", ":")) + "\n"
            if link:
                link.send(line)
            else:
                print(line, end="")
            time.sleep(1)
        return

    core = BridgeCore(cfg)
    core.start_hooks()   # localhost listener only; install stays opt-in CLI
    while True:
        pkt = core.step()
        line = json.dumps(pkt, separators=(",", ":")) + "\n"
        if link:
            link.send(line)
            if link.ser is not None and link.ser is not last_ser:
                last_ser = link.ser
                link.send(input_cfg_packet(cfg))
                send_logo(link, os.path.join(data_dir(), "logo.bin"))
        else:
            sys.stdout.write(line)
            sys.stdout.flush()
        time.sleep(core.next_interval())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
