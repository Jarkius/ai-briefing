# TODO — ai-briefing status board

> Living status file. Update whenever work lands or a check passes.
> Plans (design): `.omc/plans/2026-07-22-mcp-integration.md`, `.omc/plans/2026-07-22-control-panel.md`
> Last verified: 2026-07-23 (evidence-checked against code + logs, not assumed)

## MCP collector integration (`.omc/plans/2026-07-22-mcp-integration.md`)

Code-complete; acceptance bar not fully met.

### Done (evidence)
- [x] Phase 1 — vendor + env: `setup.sh` (3.11 venv, fork pinned @ `df43b4da`, Playwright); `data/`+`.venv/` gitignored; upstream PR #8 open
- [x] Phase 2 — `src/briefing/collector.py`: subscriptions.json reconcile + `check_feeds`
- [x] Phase 3 — `src/briefing/generator.py` + `db.py`: schema guard (`PRAGMA table_info`), `busy_timeout=15000`, style file in prompt; IMAP send pre-check in `sender.py`
- [x] Phase 4 — `src/briefing/researcher.py`: URL/YouTube/topic routing, checkbox flip (real run 2026-07-23 in `research_requests.md`)
- [x] Phase 5 — `run.py` orchestrator with `--dry-run`; plist points at `.venv/bin/python run.py` with HOME; legacy scripts → `legacy/`
- [x] End-to-end runs: 3 archives on 2026-07-23 (`archives/briefing_2026-07-23_*.md`)

### Pending
- [ ] **Reload launchd plist** — `briefing.log` 2026-07-23 06:53 shows the *legacy* pipeline still ran at schedule (DNS failures, old collectors). `launchctl unload/load` the plist, then verify via `launchctl start com.user.ai-briefing` + log inspection (plan step 13 acceptance)
- [ ] Tests: create `tests/` (request parsing, prompt assembly, fixture DB) — verification step 1
- [ ] AC5: offline soft-fail run (Wi-Fi off mid-collect → still exits 0, sends from DB)
- [ ] AC7: STYLE-MARKER-42 style-rule assert via `--dry-run`
- [ ] AC8: wall-clock measurement (<5 min no-video, <15 min with one transcription)
- [ ] Commit the staged implementation (large batch currently staged, uncommitted)
- [ ] Follow-up: repoint `setup.sh` fork SHA → upstream tag when PR #8 merges (check monthly)

## Control panel (`.omc/plans/2026-07-22-control-panel.md`)

Not started beyond scaffolding — `src/panel/` has only empty `__init__.py`, `static/`, `templates/`.

- [ ] FastAPI app + routes (preview, research/run, sources, style, schedule, settings, logs)
- [ ] Jinja + htmx templates
- [ ] `panel.sh` launcher (127.0.0.1:8787, on-demand)
- [ ] Cross-process `data/.mcp.lock` (flock) between dashboard and cron run
- [ ] In-process job registry (research/regenerate survive tab close)
- [ ] Auto-commit on dashboard saves (push stays manual)

## Housekeeping
- [ ] Branch: work is on `poc/noapi-google-search-mcp`; plans say implement on `feat/mcp-collector` — decide whether to rename/branch before PR to `main`
- [ ] Out-of-scope tracked elsewhere: 6am launchd DNS/proxy issue (`FIX_EMAIL_DELIVERY.md`)

## Work log (newest first — append a timestamped entry per session)

### 2026-07-23 22:42
- Audited MCP integration plan vs. actual code: all 5 phases implemented (collector, generator+db guards, researcher, run.py orchestrator, setup.sh with pinned fork). Evidence: 3 archives today, checked-off research request, IMAP pre-check in sender.py.
- Found gap: launchd still ran the **legacy** pipeline at 06:53 today (`briefing.log` shows old HN/RSS/GitHub collectors + DNS failures) — plist edited but not reloaded. Top pending item.
- Confirmed no tests/, AC5/AC7/AC8 unverified, implementation staged but uncommitted.
- Control panel: plan committed (`98749bd`), `src/panel/` scaffold only — no implementation.
- Created this TODO.md as the cross-machine status board.

### 2026-07-23 (earlier, prior sessions — reconstructed from git/archives)
- Implemented full `src/briefing/` package + `run.py`; moved old scripts to `legacy/`; ran pipeline end-to-end 3× (15:18, 15:29, 22:25 archives).

### 2026-07-22
- Committed MCP integration plan (`f593ab1`) and control panel plan (`98749bd`); POC of noapi-google-search-mcp tested and documented (`88073ef`).
