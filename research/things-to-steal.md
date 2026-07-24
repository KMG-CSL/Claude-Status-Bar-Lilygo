# THINGS TO STEAL
### A raid report for Claude-Status-Bar-Lilygo, from 14 comparable projects (2026-07-23)

---

## 1. TL;DR — top 5 steals, ranked by value ÷ effort

| # | Steal | From | Effort | Why it wins |
|---|-------|------|--------|-------------|
| 1 | **Hooks-push pipeline into the bridge** — install `Stop` / `UserPromptSubmit` / `PreToolUse` / `Notification` / `SubagentStop` hooks in `~/.claude/settings.json` as a marker-named one-liner that POSTs hook stdin JSON to a localhost port the bridge owns (port written to a discovery file, curl backgrounded so Claude never blocks) | [tddworks/ClaudeBar](https://github.com/tddworks/ClaudeBar) + [happy](https://github.com/slopus/happy) + [claude-notifications-go](https://github.com/777genius/claude-notifications-go) | M | Kills our #1 weakness (write-silence ambiguity) with authoritative edges: `Stop` = turn over, `UserPromptSubmit` = turn started, `Notification(permission_prompt)` = waiting-on-you. Happy's authors explicitly rejected file-watching; omnara died proving scraping loses and hooks/SDK win. |
| 2 | **Pending `tool_use` + silence ⇒ WAITING-APPROVAL, not "running"** — if newest JSONL event is an assistant `tool_use` with no matching `tool_result` after ~3.5s of write-silence, flip to orange "needs approval: `<tool>`" | [omnara](https://github.com/omnara-ai/omnara) | S | Pure-transcript, ships this week, no install step. Covers the most common blocked case even before hooks land. Calibration numbers included (3.5s debounce, 0.5s confirm, 1.0s fallback). |
| 3 | **Two-timer turn contract** — working timer runs `UserPromptSubmit → Stop`; waiting-on-you timer runs `Stop → next UserPromptSubmit`; freeze at turn end with a `completed / failed / cancelled` glyph | [ClaudeBar](https://github.com/tddworks/ClaudeBar) + [happy](https://github.com/slopus/happy) + [opcode](https://github.com/winfunc/opcode) | S | Fixes our muddy elapsed-timer semantics with one crisp rule. Works from JSONL timestamps today (elapsed = now − last user-message ts), gets exact once hooks land. |
| 4 | **Statusline pass-through collector** — wrap the user's statusline as `our-collect \| existing-statusline`; Claude Code ≥2.1.80 pushes `rate_limits` (5h/7d/per-model, `used_percentage` + `resets_at`), plus `session_id`, `context_window.used_percentage`, `cost`, `model`, `effort.level` on every refresh | [ccburn](https://github.com/JuanjoFuchs/ccburn) + [ccstatusline](https://github.com/sirmalloc/ccstatusline) | M | Replaces our Models-API context calls and dodges the persistent OAuth-usage 429 (claude-code#30930). Free per-session liveness signal as a bonus. Caveat: `rate_limits` absent on enterprise accounts — keep OAuth fallback. |
| 5 | **Escalation state machine + attention vocabulary** — fire only on upward threshold crossings / state-worsening transitions (fired-set, cleared on fallback); reserve a pulsing treatment *exclusively* for "blocked on you"; opt-in chime; firmware dead-man decay when serial goes quiet | [CodexBar](https://github.com/steipete/CodexBar) + [ClaudeBar](https://github.com/tddworks/ClaudeBar) + [CursorLight](https://github.com/JasonLam08/cursor_agent_status_light) + [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) | S | Fixes "orange banner only." The fired-set fits in bytes of firmware state; degradation-only alerts mean zero spam; dead-man decay means a dead bridge never shows a stale "running." |

Honorable mention just below the cut: **process-liveness as a third signal** (CodexBar: `ps` + `lsof cwd` → live PID + stale transcript = still running; PID gone = dead, never "running") — S effort, and it disambiguates crash-vs-long-tool even without hooks.

---

## 2. The landscape

The ecosystem splits into five categories, each with a clear standout. **Usage/quota dashboards** (standout: [CodexBar](https://github.com/steipete/CodexBar), 18.9k★) poll the OAuth usage endpoint or statusline and have the deepest rate-limit semantics but deliberately coarse agent-state models — binary active/idle at best. **Statusline formatters** ([ccstatusline](https://github.com/sirmalloc/ccstatusline), ~12k★; ccusage) are stdin-driven renderers of Claude Code's own push contract — no state inference, but they document the richest free data source we're not using. **Hook-driven notifiers** ([claude-notifications-go](https://github.com/777genius/claude-notifications-go); Claude-Code-Remote; CursorLight) prove that hook events + a turn-scoped transcript read give exact, instant state edges. **Process-owning orchestrators** ([happy](https://github.com/slopus/happy), 22.8k★; opcode; Crystal; omnara) get perfect state by wrapping the CLI or SDK — an architecture we can't adopt (it breaks "just run claude in your terminal") but whose taxonomies and turn semantics translate directly. **Hardware companions** ([Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter), 1.9k★; cc-usage-elink; CursorLight) are our literal peers — all usage- or single-session-only, none does multi-session agent state, which confirms our niche is open. The gap report adds two channels nobody in the survey uses: Claude Code's **official OTEL event export** (structured per-tool events, no hook install) and the untorn-down localhost multi-session dashboard cluster (Stargx, bruceyxli, onikan27). Meta-lesson, paid for in blood by omnara and Claude-Code-Remote: **never scrape the TUI; never regex agent prose** — use hooks, statusline, and structured JSONL fields only.

---

## 3. Steal list by theme

### 3.1 State detection

- **Hook event set + install mechanics** — [ClaudeBar](https://github.com/tddworks/ClaudeBar) `HookInstaller.swift`, [ccstatusline](https://github.com/sirmalloc/ccstatusline) `hooks.ts`, [happy](https://github.com/slopus/happy) `startHookServer.ts`. **Mechanism:** bridge binds a localhost HTTP listener (auto-port fallback), writes actual port to `~/.claude/claudestatusbar-hook-port`; installer merges a marker-named shell function (`__claudestatusbar_hook`: `cat | curl -s -X POST http://localhost:$PORT/hook -d @- >/dev/null 2>&1 &`) into `~/.claude/settings.json` for `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreToolUse`, `PostToolUse`, `Notification`. Marker name makes install/uninstall idempotent string-contains checks; tag entries (ccstatusline's `_tag`) for clean sync. Hook payload gives `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name`. **Ours:** hooks provide transitions; keep the transcript tail for the live "tool in use + detail" line — that's our differentiator ClaudeBar can't do. **Effort M, value: the whole ballgame.**
- **Instant waiting-states from `PreToolUse` matchers** — [claude-notifications-go](https://github.com/777genius/claude-notifications-go) `hooks.json`. **Mechanism:** `PreToolUse` matcher `ExitPlanMode|AskUserQuestion` fires the moment the tool is *called* — before the transcript goes silent; `Notification` matcher `permission_prompt` catches permission dialogs that never appear in the transcript at all. **Ours:** these map 1:1 to firmware states NEEDS-APPROVAL / QUESTION / PERMISSION. **S.**
- **Turn-scoped analysis window** — [claude-notifications-go](https://github.com/777genius/claude-notifications-go) `analyzer.go:74-87`. **Mechanism:** on `Stop`, filter transcript to messages after the last user timestamp, cap at last 15 — kills "ghost ExitPlanMode from three turns ago" misclassification. **Ours:** apply to the bridge's per-session state derivation. **S.**
- **Process-liveness third signal** — [CodexBar](https://github.com/steipete/CodexBar) `LocalAgentSessionScanner`. **Mechanism:** `ps -axo pid=,ppid=,lstart=,command=` filtered to basename `claude` (exclude `--version`/`--help`/`claude-code-acp`), `lsof -a -d cwd -Fn -p <pids>` for cwd, map cwd → project dir via the escape formula (non-alnum → `-`), bind only when exactly one PID per cwd and transcript mtime ≥ process start. **Ours:** live PID + stale transcript = still running (long tool call); PID gone = DEAD/DONE, never "running." Also cross-check on state transitions like opcode's `kill -0` zombie sweep. **S.**
- **Stalled-session watchdog** — [opcode](https://github.com/winfunc/opcode) `agents.rs:1013`. **Mechanism:** user message with no assistant/tool line after 30s ⇒ "stuck?" (amber); escalate at ~120s. **Ours:** per-session timer in the bridge; complements hooks for auth-broken/hung sessions. **S.**
- **Stale-state reset on session restart** — [happy](https://github.com/slopus/happy) `claudeRemoteLauncher.ts:105`. **Mechanism:** on `SessionStart` (or new PID for same project), clear any waiting-on-you state inherited from the previous incarnation — otherwise you get an unclearable orange tile. **S.**
- **Error/limit detection from JSONL flags** — [claude-notifications-go](https://github.com/777genius/claude-notifications-go) `analyzer.go:198` + [ccusage](https://github.com/ccusage/ccusage) `adapter/claude/mod.rs:630`. **Mechanism:** `isApiErrorMessage=true` + `error` field ("authentication_failed") on synthetic assistant messages ⇒ red ERROR tile; literal `Claude AI usage limit reached|<epoch>` ⇒ "limited until HH:MM" banner with a hard reset time. Two `if` statements in code we already parse. **S.**
- **Sidechain hygiene** — [ccstatusline](https://github.com/sirmalloc/ccstatusline) convention + [omnara](https://github.com/omnara-ai/omnara). **Mechanism:** exclude `isSidechain===true` from parent-session state/tool/context math; use `Task` tool_use unresolved = "delegating (N subagents)" state; happy's `docs/session-protocol-claude.md` has the `parentUuid`-ancestry fallback for when `parent_tool_use_id` is missing. **S.**
- **OTEL event stream spike** (gap report, [official docs](https://code.claude.com/docs/en/monitoring-usage)). **Mechanism:** `CLAUDE_CODE_ENABLE_TELEMETRY` + tiny local OTLP collector in the bridge ⇒ structured tool-execution/hook/API events, push-based, no settings.json surgery. Nobody in the survey uses it. **M, spike-worthy.**

### 3.2 Data & metrics

- **Full OAuth usage-endpoint field map + hardening** — [CodexBar](https://github.com/steipete/CodexBar) `docs/claude.md`, [ccstatusline](https://github.com/sirmalloc/ccstatusline) `usage-fetch.ts`, [ClaudeBar](https://github.com/tddworks/ClaudeBar). **Mechanism:** parse `seven_day_sonnet`/`seven_day_opus`, `extra_usage.{used_credits,monthly_limit}`, plan from `rate_limit_tier` (`default_claude_max_5x/_20x` → "Max 5x/20x"); TTL snapshot cache; 429 retry-at latch (observed 1h windows) serving stale data; token-hash cache invalidation; note `user:profile` scope requirement with a friendly bridge error. **Ours:** ~4 new JSON protocol fields + resilience on the endpoint we already hit. **S.**
- **5h-block timer from transcripts** — [ccusage](https://github.com/ccusage/ccusage) `blocks.rs:17` / [ccstatusline](https://github.com/sirmalloc/ccstatusline) `jsonl-blocks.ts`. **Mechanism:** block start = first entry floored to the hour; new block when gap-from-start or gap-from-last > 5h; counts only non-sidechain entries with real usage tokens. Yields "$X block · 2h45m left" offline. **S.**
- **Burn rate + pace, three flavors, pick all:** (a) ccusage's tok/min on **non-cache tokens** with 2000/5000 green/yellow/red tiers; (b) ClaudeBar's `burnRate = %used/%timeElapsed` with absolute safety nets (always critical <20% remaining) and the **hare/tortoise/equals pace glyph** (±5pt dead band); (c) Clawdmeter's ring-of-6 samples, ≥4-min span guard, 0.33 %/min anchor (= exactly window-filling pace) driving an ambient mood. **Ours:** pace glyph next to the rate-limit % first (S), then "limit at ~3:40pm" projection with ccburn's guardrails (≥3 points, ≥10% window span) (M).
- **Session-equivalents forecast** — [CodexBar](https://github.com/steipete/CodexBar) `SessionEquivalentForecast.swift`. **Mechanism:** median weekly-% per completed 5h window ⇒ "~N sessions left this week" vs windows remaining until reset. Killer single number for a desk display. **M.**
- **Context% from transcript, API as fallback** — [ccusage](https://github.com/ccusage/ccusage) `mod.rs:603`, [Crystal](https://github.com/stravu/crystal). **Mechanism:** last assistant `message.usage` (input + cache_creation + cache_read) vs model window; prefer statusline `context_window.used_percentage` when the collector lands. Kills our Models-API polling. **S.**
- **Compaction badge** — [ccstatusline](https://github.com/sirmalloc/ccstatusline) `compaction.ts`: count `type==='system' && subtype==='compact_boundary'` (non-sidechain); flash "session lost memory" with tokens reclaimed. **S.**
- **Token-speed liveness** — [ccstatusline](https://github.com/sirmalloc/ccstatusline) `speed-metrics.ts`: trailing-window t/s; visibly decays to 0 on stall — doubles as a state signal. **S.**
- **Dedup token accounting** — [opcode](https://github.com/winfunc/opcode) `usage.rs:174`: unique on `message.id:requestId`, skip all-zero usage rows — mandatory if we ever show cost totals. **S.**
- **Rollover guard** — [ccburn](https://github.com/JuanjoFuchs/ccburn) `models.py` `is_expired`: if `resets_at < now`, show 0%, not a ghost 87%. We likely have this bug today. **S.**
- **Git shortstat as progress proxy** — [omnara](https://github.com/omnara-ai/omnara): "+142 −37 in 6 files" per tile, one `git diff --shortstat` on state transitions. **S.**

### 3.3 Attention & notification UX

- **Threshold-crossing + degradation-only alerts** — [CodexBar](https://github.com/steipete/CodexBar) `SessionQuotaNotifications.swift` + [ClaudeBar](https://github.com/tddworks/ClaudeBar) `NotificationAlerter`. **Mechanism:** fire only when previous < T ≤ current; keep a fired-set, clear on fallback; severity-ordered states alert only on worsening; monotonic reset-boundary guard against countdown double-fires. **Ours:** firmware-side, a few bytes of state. **S.**
- **Attention pattern ≠ color** — [CursorLight](https://github.com/JasonLam08/cursor_agent_status_light) + [Crystal](https://github.com/stravu/crystal) `StatusIndicator.tsx` + [happy](https://github.com/slopus/happy). **Mechanism:** consistent motion grammar — slow breathe = running, sharp pulse = *blocked on you only*, static = terminal states, grey + "last seen Xm" = disconnected. Peripheral vision catches motion, not hue. **S.**
- **Three-tier escalation taxonomy** — [happy](https://github.com/slopus/happy) `pushNotifications.ts`: permission (red pulse) > question (amber) > done (green flash); done only when no queued input. Plus [claude-notifications-go](https://github.com/777genius/claude-notifications-go)'s done-flavors: `task_complete` / `review_complete` / `plan_ready` (orange — needs approval, NOT green) / `question`. **S–M.**
- **DONE-UNACKED with touch-ack** — [Crystal](https://github.com/stravu/crystal) `sessionManager.ts:240`: derived (never stored) `completed && lastViewed < updated` ⇒ pulsing "New activity" until tapped. Perfect for our touchscreen. **S.**
- **Opt-in chime / distinct per-state sounds** — [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) (reset chime, `"c":1` config flag) + Claude-Code-Remote (Glass=done, Tink=waiting) + Crystal (independent `notifyOnWaiting` / `notifyOnComplete` toggles). **Ours:** buzzer chirp on waiting-on-you, silent by default, per-event config in bridge. **S.**
- **One-line what-happened summary** — [claude-notifications-go](https://github.com/777genius/claude-notifications-go) `summary.go`: question text from `AskUserQuestion` input (60s recency gate), plan's first line from `ExitPlanMode` input, last assistant sentence for done; markdown-stripped, sentence-boundary truncation. Plus the actions footer: "1 new · 2 edit · 3 cmd · 41s". Exactly a 640px detail line. **M.**
- **Anti-flap numbers, pre-tuned** — [CursorLight](https://github.com/JasonLam08/cursor_agent_status_light) `ble_gate.py` (per-state debounce: busy 8000ms, escalations 500ms so they always punch through; sticky-busy) + notifications-go (7s/12s question-suppression windows, 2s event dedup, 180s same-content window). **Ours:** per-session send gate in the bridge; log every skip with reason. **S.**
- **Escalation ladder + away-from-desk channel** — [omnara](https://github.com/omnara-ai/omnara) (`requires_user_input` escalates, progress silent) + Claude-Code-Remote: banner → pulse → chirp after X min → optional ntfy/Telegram POST when waiting-on-you >120s. Put `needs_input:bool` + `waiting_since:ts` in the serial protocol so firmware owns the timing. **M.**
- **Focus-aware suppression** — [claude-notifications-go](https://github.com/777genius/claude-notifications-go) `focus_darwin.go`: `lsappinfo front` (no Accessibility permission) — don't buzz when the user is already looking at that terminal; grace delay so sub-N-second turns never escalate. **M.**

### 3.4 Multi-session UX

- **Fleet rollup header** — [Crystal](https://github.com/stravu/crystal) `sessionManager.ts:430`: "3▶ 1⚠ 2✔" strip; whole-fleet state without cycling. **S.**
- **Session labels that mean something** — three-source fallback: JSONL `summary` record ([happy](https://github.com/slopus/happy)) → first real user message skipping `Caveat:`/`<command-name>` noise ([opcode](https://github.com/winfunc/opcode) `claude.rs:194`, [CodexBar](https://github.com/steipete/CodexBar)'s 64-char truncation rules) → deterministic adjective/noun name from UUID ([claude-notifications-go](https://github.com/777genius/claude-notifications-go) `sessionname.go`: "bold 06ddb8f7") + git branch + folder basename. Use JSONL `cwd` field, not the lossy de-hyphenated dir name (opcode `usage.rs:163`). **S.**
- **Last prompt + last response snippet per tile** — [Claude-Code-Remote](https://github.com/JessyTsui/Claude-Code-Remote): first ~40 chars of current ask + final assistant line — makes "which session is this?" instant. **S.**
- **Claude Desktop session roots** — [CodexBar](https://github.com/steipete/CodexBar) `ClaudeDesktopProjectsLocator`: also scan `~/Library/Application Support/Claude/{local-agent-mode-sessions,claude-code-sessions}/**/.claude/projects` — we silently miss these today. **S.**
- **Destructive-tool tinting** — [opcode](https://github.com/winfunc/opcode) `checkpoint/manager.rs:718`: tool ∈ {write, edit, multiedit, bash, rm} → red/orange detail line; read-only tools grey. "Is it mutating my repo?" at a glance. **S.**
- **Reasoning-effort badge** — [ccusage](https://github.com/ccusage/ccusage): `effort.level` free in the statusline payload (CC 2.1.119+); tag expensive-thinking sessions. **S.**
- **Tap-to-focus terminal** (gap report, [onikan27/claude-code-monitor](https://github.com/onikan27/claude-code-monitor)): tap a session tile → bridge focuses that iTerm2/Terminal/Ghostty window. Natural extension of our touch gestures. **M.**

### 3.5 Hardware / protocol tricks

- **Firmware dead-man decay** — [CursorLight](https://github.com/JasonLam08/cursor_agent_status_light): no serial frame for N sec → dim + "stale Xs ago" banner → screen off. The display must never freeze on "running." **S.**
- **Device-initiated refresh** — [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter): device writes one line (`REFRESH`) upstream on the same USB CDC port on tap/wake; bridge polls immediately. Makes touch feel instant. **S.**
- **Heartbeat vs state frames** — [happy](https://github.com/slopus/happy) `docs/protocol.md`: tiny periodic `{sid, thinking}` alive frames; full JSON only on transitions; firmware derives "disconnected" from heartbeat gap. **M.**
- **USB composite CDC+HID** — [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter)'s BLE HID buttons, done better on our S3: touch button sends Shift+Tab/Escape as a USB keyboard on the same cable — display becomes a control surface with zero back-channel protocol. **M.**
- **Clock without RTC** — [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter): bridge sends local epoch (`time.time() + tm_gmtoff`) + 12/24 flag ⇒ absolute "waiting since 14:32" instead of only elapsed timers; plus ccburn/elink's formatting rule: relative under 24h ("2h 14m"), absolute beyond ("Tue 4:00 PM"). **S.**
- **Versioned schema + example payload + unknown-field tolerance** — [ccstatusline](https://github.com/sirmalloc/ccstatusline): commit `payload.example.json`, validate, degrade gracefully — lets bridge and firmware evolve independently. **S.**
- **Never push an error frame** — [ccusage](https://github.com/ccusage/ccusage) cache discipline: hold last-good state with an age flag instead of blanking a widget on API hiccups. **S.**
- **Tailer hardening bundle** — file-size-delta before parse (opcode), seek-to-offset delta reads (opcode's own O(n²) bug inverted), `uuid`-keyed line dedup for restart safety (happy `sessionScanner.ts`), scan budgets 512 entries/250ms + future-mtime clamp (CodexBar). **S.**

### 3.6 Install & config UX

- **Idempotent hook install with self-test** — [CursorLight](https://github.com/JasonLam08/cursor_agent_status_light) `install-cursor-light.sh`: ship a settings snippet, merge never clobber, end with a visible hardware self-test ("screen flashes green" = install verified). **M.**
- **`--describe` for agent-driven setup** — [ccburn](https://github.com/JuanjoFuchs/ccburn): emit JSON with exact settings.json path + before/after snippet; README says "paste `npx -y ccburn describe` into Claude Code and ask it to configure." Let Claude install our hooks. **S.**
- **Zero-config token discovery** — [cc-usage-elink](https://github.com/fuergaosi233/cc-usage-elink) `_detect_token_from_keychain`: config file → `security find-generic-password -s 'Claude Code-credentials' -w` → `json.claudeAiOauth.accessToken`; plus the documented `claude setup-token` scope-rewrite (`user%3Ainference%20user%3Aprofile`) for minting a usage-capable token. **S.**
- **Service polish** — [Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter): one-command per-OS installers (LaunchAgent / systemd --user / HKCU Run + tray icon with green/amber/red daemon health). Adoption-critical. **M.**
- **Self-probe loopback filter** — [ClaudeBar](https://github.com/tddworks/ClaudeBar) issue #172: if the bridge ever shells out to `claude`, run it in a dedicated dir and filter hook/transcript events by that cwd suffix — no phantom 9th session. **S.**
- **CLAUDE.md as hardware lab notebook** — [cc-usage-elink](https://github.com/fuergaosi233/cc-usage-elink): document serial protocol, LilyGo touch quirks, symptom→cause→fix table so every future Claude session on the repo is instantly competent. **S.**
- **Adaptive poll cadence** — [CodexBar](https://github.com/steipete/CodexBar) `AdaptiveRefreshPolicyCore`: named-reason decision table (2/5/15/30min by warmth; any activity <5min caps delay at 5min); ClaudeBar's sleep/wake pause + refresh-burst-on-wake, telling firmware to dim while the Mac sleeps. **S.**

---

## 4. Deliberate non-goals

- **TUI/terminal scraping in any form** (omnara's `"esc to interrupt"` matching, Claude-Code-Remote's `/done/i` regexes, ClaudeBar's keystroke auto-responders) — omnara deprecated its entire repo because this is unmaintainable; structured JSONL + hooks only.
- **Regexing agent prose for state** (CursorLight's 50-line bilingual plan-detection swamp) — Claude Code gives us structured `ExitPlanMode`/`AskUserQuestion` tool names; never infer from natural language.
- **Owning/wrapping the claude process** (opcode, Crystal, happy remote-mode, omnara) — forces users to launch through us; passive tail + hooks keeps zero-friction. Optional `--session-id` launcher alias at most.
- **Burning real API tokens to read rate-limit headers** (Clawdmeter's 1-token Haiku call/60s with spoofed UA) — costs quota; OAuth/statusline are strictly better.
- **Browser-cookie decryption as a usage source** (CodexBar, ccburn's Cloudflare-dodging curl) — fragile, privacy-sensitive, ToS-gray, redundant.
- **Auto-approving permission dialogs** (Claude-Code-Remote sends "Yes, don't ask again") — a status device must never silently defeat the permission model.
- **Free-text remote command injection with regex blacklists** (Claude-Code-Remote's email listener) — if we ever accept inbound actions: constrained verbs (approve/deny), tokened, expiring.
- **BLE as primary transport** — half of Clawdmeter's daemon is CoreBluetooth pathology management; USB serial avoids the entire tax.
- **Provider/widget/adapter sprawl** (CodexBar's 63 providers, ccstatusline's 90 widgets, ccusage's 20 CLI adapters) — hard-pick ~10 data points for one provider.
- **Binary active/idle as the state model** (CodexBar's 120s mtime window) — we're already ahead; steal their signals, not their state machine.
- **Weekly depletion predictions from naive regression** — ccburn measured 16.5h mean error on bursty weekly windows; project the 5h window only.
- **Heavyweight forecasting** (Holt-Winters, Chronos-T5) and **replay-audit rigs** (AdaptiveReplayKit) — overkill for a desk display.
- **Skipping tool-level hooks like ClaudeBar does** — their lifecycle-only event list can't show tool-in-use or permission-waits; those are our differentiators.
- **Unofficial hook names registered blind** (ClaudeBar's `TaskCompleted`/`SubagentStart`) — verify against current docs before wiring firmware states.
- **Megabyte baked-asset C arrays in git** (Clawdmeter's 1MB+ fonts) — build-time asset pipeline instead.

---

## 5. Appendix — full project table

| Project | URL | Category | State-detection mechanism | Verdict |
|---|---|---|---|---|
| CodexBar | [github](https://github.com/steipete/CodexBar) | Usage dashboard (menubar) | Process scan (`ps`+`lsof cwd`) + transcript-mtime correlation; OAuth usage API polling with adaptive cadence | Best quota semantics + the process-liveness trick; state model too coarse to copy |
| tddworks/ClaudeBar | [github](https://github.com/tddworks/ClaudeBar) | Usage + session monitor (menubar) | Pure hooks-push over localhost HTTP with port-discovery file; zero tailing/polling | The hook-pipeline blueprint; single-session and no tool detail |
| Clawdmeter | [github](https://github.com/HermannBjorgvin/Clawdmeter) | Hardware usage gauge (ESP32/BLE) | None — polls Messages API for rate-limit headers (avoid); firmware-side burn-rate ring | Our closest hardware peer; steal device-refresh, chime, clock, HAL — not its telemetry |
| CursorLight | [github](https://github.com/JasonLam08/cursor_agent_status_light) | Hardware status light (Cursor) | 100% Cursor Hooks → bash orchestrator → flock'd debounce gate → BLE | Frozen 2-day project, but the debounce gate + dead-man decay + attention vocabulary are gold |
| ccusage (statusline) | [github](https://github.com/ccusage/ccusage) | Statusline cost/budget | None — statusline stdin contract + JSONL 5h-block/burn-rate math | The budget layer we lack: block countdown, non-cache burn tiers, limit-reset sniffing |
| ccstatusline | [github](https://github.com/sirmalloc/ccstatusline) | Statusline formatter | None — statusline stdin (Zod-validated) + managed tagged hooks side-channel | Richest documentation of the free statusline payload; steal acquisition layer, skip rendering |
| Claude-Code-Remote | [github](https://github.com/JessyTsui/Claude-Code-Remote) | Remote notifier + reply | Stop/SubagentStop hooks; legacy tmux pane scraping (avoid) | Hacky code, good ideas: per-state sounds, prompt/response snippets, action tokens |
| claude-notifications-go | [github](https://github.com/777genius/claude-notifications-go) | Hook-driven notifier | Plugin hooks (PreToolUse matchers, Notification, Stop) + turn-scoped one-shot transcript analysis | The best state classifier in the survey; steal its taxonomy, window, and summaries wholesale |
| ccburn | [github](https://github.com/JuanjoFuchs/ccburn) | Usage TUI + trajectory | None — statusline pass-through collector → SQLite; OAuth/web fallbacks | Statusline collector + pace/projection math with honest accuracy postmortems |
| cc-usage-elink | [github](https://github.com/fuergaosi233/cc-usage-elink) | Hardware usage (e-ink/BLE) | None — OAuth usage polling only | Dormant hackathon; steal keychain token discovery + uv single-file distribution |
| happy | [github](https://github.com/slopus/happy) | Process-owning remote client | SDK `canUseTool` callback + injected SessionStart hooks + localhost hook server; explicit anti-file-watching stance | Most mature; its architecture is the proof hooks > tailing; steal taxonomy + protocol docs |
| opcode (ex-Claudia) | [github](https://github.com/winfunc/opcode) | Process-owning GUI | Spawns claude, supervises PID + stream-json; 30s first-output watchdog; SQLite run lifecycle | Dormant; steal the watchdog, PID cross-check, dedup accounting, 5-state lifecycle |
| Crystal (Nimbalyst) | [github](https://github.com/stravu/crystal) | Process-owning orchestrator | PTY + stream-json parsing; MCP permission bridge = exact waiting state | Frozen reference; steal completed_unviewed, animation grammar, rollup header |
| omnara | [github](https://github.com/omnara-ai/omnara) | Process-owning control plane | PTY scrape (killed the project) + deterministic `--session-id` transcript binding + tool_use+silence inference | DEPRECATED — its death is the lesson; its heuristics (steal #2) live on |

Gap-report follow-ups not yet torn down: [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) (~8.5k★, canonical burn-rate predictor), [Stargx/claude-code-dashboard](https://github.com/Stargx/claude-code-dashboard) and [bruceyxli/claude-code-monitor](https://github.com/bruceyxli/claude-code-monitor) (our closest architectural competitors — multi-session, subagents, audio alerts, remote approval), [onikan27/claude-code-monitor](https://github.com/onikan27/claude-code-monitor) (terminal focus switching), plus the official **OTEL export** and **Remote Control** docs — the first-party absorption of this whole category is underway; our moat is the glanceable hardware form factor.

---

## 6. Round 2 - the direct competitors (gap-fill dives)

Round 2 covered the four flagged gaps: Stargx/claude-code-dashboard, onikan27/claude-code-monitor (ccm), Maciek-roboblog/Claude-Code-Usage-Monitor, and the official OTEL telemetry channel. Verdict up front: **nothing in round 2 dethrones the hooks pipeline or pending-tool_use waiting detection already in this report** — both hook-driven competitors have *weaker* waiting detection than our plan, and OTEL provably cannot see a pending permission prompt at all (tool_decision fires only after the user answers). What round 2 adds is accounting correctness, slot hygiene, and two genuinely new display states (rate-limited, API-retrying). Steals below are only the ones NOT already covered by sections 1-5.

### 6.1 Stargx/claude-code-dashboard — [repo](https://github.com/Stargx/claude-code-dashboard)

One-day project (built and abandoned 2026-03-09, 10 stars) tailing the same JSONL we do, with a text-heuristic "waiting" detector strictly worse than ours — mine it once and move on. Its read-time status decay (<15s active, >60s idle, recomputed at poll time) independently validates our two-timer contract with near-identical thresholds. The real value is clean accounting and slot-hygiene primitives.

| Steal | Mechanism | Effort | Value |
|---|---|---|---|
| Per-message-id token delta dedup | `seenMessageIds: msg.id → last cumulative usage`; add only `max(0, curr−prev)` per field. Claude Code writes repeated JSONL events per assistant message with *cumulative* usage — naive summing double-counts. (watcher.js) | S | High if we show tokens/cost |
| Context-window gauge | `lastTurnInputTotal = input + cache_creation + cache_read` of newest assistant usage → one int over serial → 2-3px bar per session row; warns of imminent auto-compact | S | High — cheapest new signal for the TFT |
| Idle dedup + stale tier | Collapse idle sessions to newest-per-project, hide idle when project has an active session, dim anything untouched since local midnight. Effectively increases our 8-slot capacity | S | High |
| agentId subagent liveness | Subagent = event with `agentId` NOT starting `'acompact'`; skip `.jsonl` basenames containing `'compact'`; count only subagents with events in last 15s. Makes our count mean "live now", kills phantom compaction agents | S | Med-High |
| Noise-event skip list | Ignore `file-history-snapshot`, `queue-operation`, `last-prompt` before touching `lastEventAt` — they fire without real activity | S | Medium |
| permissionMode badge | JSONL events carry `permissionMode` verbatim → red glyph for `bypassPermissions`, yellow for `acceptEdits`. Zero heuristics | S | Medium |

**Avoid:** its 0.25x cache-write pricing (Anthropic charges 1.25x); its offset map never resets on file shrink — ours must; its parse-time-frozen subagent status (stale-thinking bug).

### 6.2 onikan27/claude-code-monitor (ccm) — [repo](https://github.com/onikan27/claude-code-monitor)

Hook-driven macOS TUI + phone remote, 268 stars, dormant since 2026-01-29. Its `Notification`/`permission_prompt` hook and Stop-edge detection are already covered by our hooks-pipeline section — ccm is production proof the design works, nothing more to add there. Its unique, unclaimed territory is TTY plumbing: liveness, eviction, and focus-by-TTY.

| Steal | Mechanism | Effort | Value |
|---|---|---|---|
| TTY-liveness GC | Delete a session the instant its `/dev/ttysNNN` stops `stat()`ing (tty-cache.ts, 30s cache). We can get the TTY hook-free: `lsof` on the transcript → claude PID → `ps -o tty=`. "Row disappears" = terminal actually closed, not "idle 30min". Kills ghost sessions — our top slot-hygiene complaint | M | High |
| Sticky-done rule | Once `stopped`, only `UserPromptSubmit` may resume it; late PostToolUse/summary flushes can't flip Done→Running (file-store.ts:129-136). Kills done→running→done flicker | S | Medium |
| Button → focus terminal | Lilygo button sends `{"focus": slot}` up serial; bridge maps slot→tty→osascript (iTerm2/Terminal iterate tabs matching `tty of aSession`, focus.ts:77-116; Ghostty via OSC-0 title tag, best-effort). Turns the display into a desk KVM — no one else has this | L | High — strongest differentiator available |
| Debounce numbers | 100ms write coalesce, 150ms read debounce, 60s reconciliation sweep — apply to serial frame coalescing to stop LCD churn during rapid tool loops | S | Medium |

**Avoid:** hooks-only state (loses tool name/detail/subagents between edges); clipboard-stomping text injection; `unknown TTY = alive forever` (fall back to mtime idle instead).

### 6.3 Maciek-roboblog/Claude-Code-Usage-Monitor — [repo](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)

The only *alive* project in the round (8.5k stars, pushed 2026-07-05), and it has zero session-state logic — pure rate-limit/burn-rate dashboard. Its statusline capture is already in our statusline-collector section; new here is its **hardening** and the limit/burn math. This repo contributes an entirely new display state: RATE-LIMITED with an exact countdown.

| Steal | Mechanism | Effort | Value |
|---|---|---|---|
| Rate-limit-hit detection w/ exact reset epoch | Two regexes over lines we already parse: tool_result text `r"limit reached\|(\d+)"` → exact Unix reset epoch; system msgs `r"wait\s+(\d+)\s+minutes?"` → reset = ts + N min (analyzer.py:386-420). Flip tile to RATE-LIMITED + live countdown instead of confusing silent idle | S | High — new attention state, exactly our display's job |
| Statusline-capture hardening | Retrofit onto our collector: atomic write via pid-unique tmp + `os.replace`, 600s freshness TTL, drop `used_percentage > 101` (leak bug #52326), null pct once `now >= resets_at`, tombstone on plan downgrade (official.py:42,115-144) | S | Medium — correctness insurance |
| Burn rate + exhaustion projection | ~60 lines (calculations.py:34-91): tokens incl. cache / block minutes, project to block end, warn if projected exhaustion < reset. Hourly rate prorates each session by overlap with trailing 60min — correct for our 8-concurrent-session case | M | Med-High |
| 5h billing-block algorithm | Start = first entry floored to UTC hour, end = +5h, split on gap ≥ 5h, active = `end > now` (analyzer.py:124-165). Fallback reset clock when no statusline capture | S | Medium |
| Cross-file dedup key | `f"{message_id}:{request_id}"` when aggregating across files — resumes/rewrites duplicate API calls across JSONLs (reader.py:263-272). Complements Stargx's within-file delta dedup | S | Medium |
| Persisted alert cooldown | Per-alert `{triggered, timestamp}` JSON, 24h cooldown — bridge restart doesn't re-fire the buzzer (notifications.py:78-98) | S | Low-Med |

**Avoid:** full rglob+reparse every 10s (keep our incremental tail); hardcoded plan limits (self-labeled "unverified"); its `SessionMonitor` (billing blocks only, despite the name).

### 6.4 Official OTEL telemetry — [docs](https://code.claude.com/docs/en/monitoring-usage), live-verified

First-party structured events, verified end-to-end with a working stdlib-only prototype (`/private/tmp/claude-1668688456/-Users-u0145206-projects-Claude-Status-Bar-Lilygo/6d9f0109-4dd0-4d50-94a6-4652fb2b8ff7/scratchpad/otlp_sink.py`). Definitive answer for the report: **OTEL is an enrichment sidecar, never a state channel** — logs batch at 1-5s and no event fires while a permission prompt is *pending*. Strictly additive, opt-in via 5 env vars.

| Steal | Mechanism | Effort | Value |
|---|---|---|---|
| ~60-line OTLP/HTTP-JSON sink in the bridge | `ThreadingHTTPServer` POST `/v1/logs`, gunzip, walk `resourceLogs[].scopeLogs[].logRecords[].attributes`, key on `event.name` + `session.id` (same UUID as JSONL filename — joins for free). No protobuf/grpc; `http/json` is officially supported | S | High |
| Per-session cost ticker | `api_request` carries pre-computed `cost_usd` + tokens per call — no pricing table to maintain. One `cost_cents` field per session over serial | M | High — the #1 ambient stat we lack |
| Approval provenance | `tool_decision.source` (`config` vs `user_temporary/permanent` vs `user_abort/reject`) confirms *why* waiting cleared — flash green/red — and auto-validates our waiting heuristic (flagged waiting but `source=config` = false positive) | S | Med-High |
| API-distress state | `api_error` with `status_code`/`attempt` → "API retrying" badge; a 429/529 stall is invisible in JSONL until the request resolves. Second new display state from round 2 | S | Medium |
| Subagent cross-check | `api_request.query_source` (`main`\|subagent-name) + `agent.name` = ground-truth subagent attribution to validate our Task-block counting | S | Medium |

**Avoid:** any reliance on OTEL for waiting detection; the beta traces pipeline; grpc/protobuf protocols; content-logging env vars; making OTEL required (env vars must precede launch — blank displays for fresh installs).

### Did round 2 change the top-5 ranking?

Mostly no — and that is itself a finding: round 2 was supposed to surface our closest direct competitors, and instead it confirmed that hook-based waiting detection (#1) and the pending-tool_use/JSONL state machine (#2) remain the right core, with ccm serving as production proof rather than a rival design and OTEL confirmed as constitutionally unable to compete on the waiting edge. Two changes are warranted though. **In:** the RATE-LIMITED state with exact reset epoch (Maciek's two regexes, S-effort, converts the single most confusing silent stall into an explicit countdown) deserves a top-5 slot. **Rising but not top-5:** TTY-liveness GC + idle dedup as a combined "slot hygiene" item — biggest quality-of-life win for an 8-slot display — sits at #6. Revised top-5: (1) hooks pipeline for waiting/Stop edges, (2) pending-tool_use waiting detection as fallback, (3) statusline rate_limits collector now with Maciek's hardening, (4) two-timer contract with read-time decay at emit, (5) RATE-LIMITED state with live countdown. The button-to-focus-terminal KVM feature is the most exciting *new* idea of the round, but it's L-effort and a differentiator, not a correctness fix — roadmap it separately.

> Round-2 note: the bruceyxli/claude-code-monitor dive failed (agent output error) - its headline features (WebSocket live state, audio permission alerts, remote approval) are captured in research/survey-gaps.md for a future look.
