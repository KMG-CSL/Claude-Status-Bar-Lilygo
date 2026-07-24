# Completeness critique: genuine gaps

## 1. Official Claude Code features we under-weighted

- **OpenTelemetry export (official)** — Claude Code natively exports metrics *and events* (tool executions, hook firings, API requests) via `CLAUDE_CODE_ENABLE_TELEMETRY` + OTLP ([docs](https://code.claude.com/docs/en/monitoring-usage)). This is a supported, structured, push-based event stream — a third detection path besides hooks and transcript-tailing that nobody in our survey uses. A tiny local OTLP collector feeding the bridge would give per-tool events without installing hooks into every project. Worth a spike.
- **Remote Control / claude.ai/code (official, Feb 2026 research preview)** — Anthropic now ships its own "watch your local session from another device" layer ([docs](https://code.claude.com/docs/en/remote-control)). It doesn't expose a public status API for us to consume, but it is the official answer to the Happy/omnara/Claude-Code-Remote category and partially obsoletes them; the survey should note that this category is being absorbed first-party.
- Statusline API, hooks, and Agent SDK were already correctly identified in the survey — no gap there beyond confirming hooks remain the sanctioned fix for write-silence.

## 2. Conspicuously absent projects

- **Maciek-roboblog/Claude-Code-Usage-Monitor** (~8.5k stars, [repo](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)) — arguably THE canonical rate-limit monitor alongside ccusage, with burn-rate **predictions and warnings** (ML-ish session-limit estimation). Bigger than ccburn/Clawdmeter in its niche; omitting it while including ccburn is the survey's clearest miss.
- **The "multi-session localhost dashboard" cluster** — direct feature-for-feature competitors to our exact product (multi-session, tool-in-use, subagent counts, waiting-for-approval) that the survey skipped entirely:
  - [Stargx/claude-code-dashboard](https://github.com/Stargx/claude-code-dashboard) — tokens, costs, active tools, **subagents**, session status across all terminals; no-build Node file-reader (same architecture as our bridge).
  - [bruceyxli/claude-code-monitor](https://github.com/bruceyxli/claude-code-monitor) — live state via WebSocket, **audio alerts on permission requests, remote approval** — exactly the escalation feature we lack.
  - [onikan27/claude-code-monitor](https://github.com/onikan27/claude-code-monitor) — mobile web UI + **terminal focus switching** (iTerm2/Terminal/Ghostty), i.e., tap-session-to-jump — an obvious steal for our touch gestures.
  - [sverrirsig/claude-control](https://github.com/sverrirsig/claude-control) and [yepzdk/claude-sessions-monitor](https://github.com/yepzdk/claude-sessions-monitor) — auto-discovery of running sessions.
  At least Stargx and bruceyxli deserve full teardowns; they solve our exact state-derivation problem.
- **Multi-agent orchestrators with exact state**: [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) (Rust/TS kanban over Claude Code/Codex/Gemini; company shut down early 2026 but community-maintained) and **claude-squad** (tmux+worktree parallel agent manager). Crystal is in the survey but these two are the bigger names in the "owns-the-process, exact state" category.

## 3. Non-Claude ecosystems worth one look

- **Devin Desktop "Agent Command Center"** — a kanban of every running agent sorted by *in progress / blocked / ready-for-review* ([overview](https://apidog.com/blog/whats-new-in-devin-2026/)). "Blocked vs ready-for-review" is a cleaner top-level taxonomy than our five states and maps well to a glanceable 640x180 display.
- **Cursor background agents (April 2026 overhaul)** — Agent Tabs grid of parallel agent conversations ([guide](https://www.deployhq.com/guides/cursor)); Cursor Hooks are already represented via CursorLight, so this is a UX reference only.
- Copilot Workspace: nothing distinctive found for status UX; safe to skip.

## Bottom line
Two real omissions: (1) the official **OTEL event stream** as a detection channel, and (2) the **localhost multi-session dashboard cluster** (Stargx, bruceyxli, onikan27) + **Claude-Code-Usage-Monitor**, which are the closest things to direct competitors and were entirely absent. Everything else in the survey holds up.

Sources: [Claude Code Monitoring docs](https://code.claude.com/docs/en/monitoring-usage), [Remote Control docs](https://code.claude.com/docs/en/remote-control), [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor), [Stargx/claude-code-dashboard](https://github.com/Stargx/claude-code-dashboard), [bruceyxli/claude-code-monitor](https://github.com/bruceyxli/claude-code-monitor), [onikan27/claude-code-monitor](https://github.com/onikan27/claude-code-monitor), [sverrirsig/claude-control](https://github.com/sverrirsig/claude-control), [yepzdk/claude-sessions-monitor](https://github.com/yepzdk/claude-sessions-monitor), [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban), [Devin 2026](https://apidog.com/blog/whats-new-in-devin-2026/), [Cursor guide](https://www.deployhq.com/guides/cursor)