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
- [ ] **⚠️ Remaining risk for 5am email: Gmail SMTP/IMAP TLS-blocked on this network** (2026-07-24 daytime: TCP connects, TLS handshake reset 6/6 on :465/:993/:587; HTTPS :443 to Google works fine). Historical sends succeeded, so the block may not apply at 5am. If tomorrow's email doesn't arrive with "Connection reset by peer" in briefing.log → implement Gmail-API-over-443 send fallback (needs OAuth setup).
- [ ] Gemini free-tier key (in .env) hit 429 daily/rate quota during 2026-07-24 testing — quota resets midnight PT (=2pm Bangkok); 5am run should have fresh quota. maxplus commented out in .env (pool no longer serves gemini models; would need MAXPLUS_MODEL=gpt-5.5 + credit top-up to re-enable).
- [ ] AC5: offline soft-fail run (Wi-Fi off mid-collect → still exits 0, sends from DB)
- [ ] AC7: run `scripts/check_style_marker.sh` (harness written, unexecuted — blocked on provider 402)
- [ ] AC8: wall-clock measurement (<5 min no-video, <15 min with one transcription) — blocked on provider 402
- [ ] QA gate report → PR `feat/mcp-collector` → `main`
- [ ] Follow-up: repoint `setup.sh` fork SHA → upstream tag when PR #8 merges (check monthly)

### Done 2026-07-23 late session (team run)
- [x] launchd reloaded; `launchctl start` runs the NEW pipeline (log 23:10/23:12/23:22: Collect→Research→Generate phases) — legacy path gone
- [x] Schedule moved 06:00 → **05:00** per user request; plist reinstalled + reloaded (verified via `plutil`)
- [x] Implementation committed + pushed on `feat/mcp-collector` (`4bd6aca`), merged `origin/main` Windows work (`68edb81`)
- [x] `tests/` — 54 tests green in <1s, no network (`22d63a9`, extended in `a35aa74`)
- [x] Windows wrapper now targets `run.py`; AC7 harness written (`f2ce5ac`)
- [x] Fixed: bare-URL source links stripped by sanitizer → `[title](url)` (`c6e1d1c`); SMTP retry + 465/587 fallback (`c6e1d1c`)
- [x] Fixed: `src/briefing/config.py` was never tracked (bare `config.py` gitignore rule) — fresh clones were broken; rule scoped to root, module committed (`a35aa74`)
- [x] Gemini-direct fallback + fail-fast on 4xx in generator (`a35aa74`)

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

### 2026-07-24 ~09:00 (continuation of team session)
- **5am run verified end-to-end via launchd**: schedule fired, NEW pipeline ran (39 items collected), only Generate failed — HTTP 402 maxplus credit. Email absent for that reason alone.
- QA gate: two independent reviews returned. Fixed all actionable findings (`876e288`): email HTML escaping + link-scheme allowlist, dead card-body loop that dropped every social post, IMAP emoji UnicodeEncodeError that would fail every send, Windows fcntl import crash, Gemini key to header, .env parser quotes/comments, either-provider require_env, minimal MCP subprocess env, harness backup verification, prompt-injection hardening.
- 54 tests green after all fixes; functional render checks pass (social posts render, XSS escaped, ascii IMAP marker).

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

### Network facts (confirmed 2026-07-24)
- **Office network allows ONLY port 443** — Gmail SMTP (465/587) and IMAP (993) TLS handshakes are reset; Gemini API over 443 works fine. Confirmed by A/B test: same send failed on office network, succeeded instantly on mobile hotspot (both parts delivered).
- Consequence: scheduled 5am send only works when the Mac is NOT on the office network, until a Gmail-API-over-443 send fallback is implemented (needs one-time OAuth consent from user).
