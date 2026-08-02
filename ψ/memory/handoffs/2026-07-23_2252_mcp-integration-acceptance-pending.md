---
author: claude-fable-5
machine: Chakkrits-MacBook-Air
session: 66f22bfa
date: 2026-07-23
project: ai-briefing
privacy: repo-safe
---

# Handoff — ai-briefing: MCP integration acceptance pending

## Goal
Replace hand-rolled collectors with the vendored noapi-google-search-mcp feed layer per `.omc/plans/2026-07-22-mcp-integration.md`, then build the control panel per `.omc/plans/2026-07-22-control-panel.md`.

## Completed
- All 5 implementation phases of the MCP plan (evidence-audited 2026-07-23 22:40):
  - `setup.sh` — 3.11 venv, fork pinned @ `df43b4da`, Playwright Chromium
  - `src/briefing/collector.py` — subscriptions.json reconcile + check_feeds
  - `src/briefing/generator.py` + `db.py` — PRAGMA table_info schema guard, busy_timeout=15000, newsletter_style.md in prompt
  - `src/briefing/sender.py:178` — IMAP cross-machine send pre-check
  - `src/briefing/researcher.py` — YouTube/URL/topic routing, checkbox flip (real run: research_requests.md has 2026-07-23 checked item)
  - `run.py` — collect→research→generate→send orchestrator with `--dry-run`
- 3 end-to-end runs on 2026-07-23 (`archives/briefing_2026-07-23_{1518,1529,2225}.md`)
- `TODO.md` created at repo root — cross-machine status board with work log (untracked)

## Not yet done
1. **launchd reload + acceptance** (top priority): plist edited but never reloaded — 06:53 scheduled run executed the LEGACY pipeline (see briefing.log). Do `launchctl unload/load ~/Library/LaunchAgents/com.user.ai-briefing.plist` (or its actual location), then `launchctl start com.user.ai-briefing`, then check briefing.log shows the new pipeline
2. Commit the staged implementation (~1,300 lines staged, uncommitted, on branch `poc/noapi-google-search-mcp`) + TODO.md — needs user go-ahead
3. `tests/` — pytest for request parsing, prompt assembly, fixture DB (plan verification step 1)
4. Acceptance criteria AC5 (offline soft-fail), AC7 (STYLE-MARKER-42 dry-run assert), AC8 (wall-clock <5min/<15min)
5. Control panel — `src/panel/` is empty scaffold; whole plan pending
6. Follow-up: repoint setup.sh fork SHA → upstream tag when upstream PR #8 merges

## Failed approaches
- `/inbox` skill dead-ends in this repo — no `ψ/inbox/` exists (not an Oracle-scaffolded repo). Orient via `TODO.md`, git status, and `.omc/plans/` instead.
- (From logs, prior sessions) Legacy pipeline's 06:00 runs fail with DNS errors (`nodename nor servname provided`) — tracked out-of-scope in `FIX_EMAIL_DELIVERY.md`; the new pipeline's soft-fail design is the mitigation, not a DNS fix.

## Key decisions
- Repo-root `TODO.md` (committed, once user approves) is the progress tracker — chosen over vault because the two-computer workflow syncs via git
- Work log entries dated 2026-07-22/23 before this session are reconstructed from git/archives, not primary records

## Current state
Branch `poc/noapi-google-search-mcp`, large staged-uncommitted batch (see `git status`). Plans said implementation goes on `feat/mcp-collector` — branch naming unresolved. `data/feeds.db` exists locally (gitignored, per-machine).

## Files to know
- `TODO.md` — start here
- `.omc/plans/2026-07-22-mcp-integration.md`, `.omc/plans/2026-07-22-control-panel.md`
- `run.py`, `src/briefing/*.py`, `setup.sh`, `com.user.ai-briefing.plist`
- `briefing.log` — proof of the launchd gap

## Resume instructions
Read `TODO.md`, then do "Not yet done" #1 (launchd reload + verify). Ask user about #2 (commit + branch name) before committing.

## Warnings
- Do NOT trust `briefing.log` success as new-pipeline evidence until after the plist reload — it currently logs legacy runs
- `data/` and `.venv/` are per-machine; never commit
- User rule: nothing deleted, archive instead; always feature branches, never direct main
