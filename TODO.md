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
- [x] ~~Remaining risk for 5am email: Gmail SMTP/IMAP TLS-blocked on this network~~ **RESOLVED 2026-07-25**: Gmail API over 443 is now the PRIMARY send+pre-check transport (SMTP/IMAP demoted to fallback). OAuth done on this Mac as sender `jarkius.ai@gmail.com`; real sends verified through the API path. Note: Windows machine still needs its own OAuth token (data/ is per-machine) — until then it uses SMTP→Outlook COM as before.
- [ ] Gemini free-tier key (in .env) hit 429 daily/rate quota during 2026-07-24 testing — quota resets midnight PT (=2pm Bangkok); 5am run should have fresh quota. maxplus commented out in .env (pool no longer serves gemini models; would need MAXPLUS_MODEL=gpt-5.5 + credit top-up to re-enable).
- [x] AC5 **PASSED 2026-07-25 20:26**: Wi-Fi killed 8s into Collect (`networksetup -setairportpower en0 off`, 25s outage) — all 26 sources logged per-source DNS soft-fails (no traceback), pipeline continued, generated from 48 DB items, Send returned `{'part1': 'already_sent', 'part2': 'already_sent'}` (correct dedup — 4 sends already that day), **exit 0**. Wi-Fi restored via trap.
- [x] AC7 **PASSED 2026-07-25 20:25**: `scripts/check_style_marker.sh` exit 0 — STYLE-MARKER-42 in `archives/briefing_2026-07-25_2025.md`, newsletter_style.md restored byte-identical. (Harness itself had a bug: "Archived to" log line now lists 3 comma-separated paths; sed fixed to take the first.)
- [x] AC8 **MET (no-video bound)**: two full scheduled launchd runs measured via briefing.log phase timestamps — 19:05:06→19:06:19 (73s) and 20:13:02→20:14:03 (61s), both ≪ 5-min bar. With-one-transcription bound (<15 min) untested — no new YouTube video available on test days; bound remains theoretical.
- [x] QA gate #2 done 2026-07-25 (findings fixed in `a03c8b3`: Windows %-d strftime crash, empty-recipient guard); **PR #2 open with full acceptance evidence** → https://github.com/Jarkius/ai-briefing/pull/2 — awaiting user review/merge
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

**v1 BUILT 2026-07-25** on branch `feat/control-panel` (stories S1–S6 in `.omc/plans/2026-07-25-control-panel-stories.md`; commits 5f30674…9914b8e). 136 tests green; all 7 tabs live-verified on :8787.

- [x] FastAPI app + routes (preview, research/run, sources, style, schedule, settings, logs)
- [x] Jinja + htmx templates + dark editorial CSS base
- [x] `panel.sh` launcher (127.0.0.1:8787, on-demand; test pins the bind)
- [x] Cross-process `data/.mcp.lock` — research job acquires inside the task; route pre-rejects via is_locked()
- [x] In-process job registry — blocking core work via asyncio.to_thread (loop-not-blocked test); research reattaches on reload (AC7)
- [x] Auto-commit on dashboard saves — pathspec-restricted, never bare; push stays manual
- [x] AC2 **PASSED 2026-08-02**: `curl http://192.168.0.109:8787/preview` (real LAN IP, not loopback) → connection refused (exit 7); localhost `curl http://127.0.0.1:8787/preview` → 200. Confirms the panel is genuinely bound to 127.0.0.1 only.
- [x] AC6 **PASSED 2026-08-02**: submitted a real YouTube URL via `/research/run` against the live :8787 instance — transcribed successfully, banner showed "✓ research done" with the transcript.
- [x] AC7 **PASSED 2026-08-02**: raced a slow (non-cached) video download against an immediate `/research` page reload — the reloaded page correctly showed the live job's banner with its `hx-get`/`hx-trigger` polling attributes, proving in-flight-job reattachment works without needing the specific job URL.
- [x] AC8 **PASSED 2026-08-02**: `kill -9` on the live panel process mid-job. Verified after restart: the pending `research_requests.md` line stayed unchecked (no corruption/partial write), and `data/.mcp.lock` released cleanly (`is_locked() == False`) despite the hard kill. Panel restarted immediately after (was in active use).
- [ ] AC5 (IMAP before/after send count) still needs live proof — blocked right now by a separate, real issue found 2026-08-02: the Gmail API OAuth token has been revoked (`invalid_grant`) and the SMTP/IMAP fallback's app password is also stale for the current sending account, so every real send currently fails on both paths. See README "Troubleshooting → invalid_grant" — fix needs a human browser consent click (`scripts/setup_gmail_oauth.py`), cannot be automated.
- [ ] S6 polish backlog: full letter-texture theme pass (base theme shipped), run-history table (deferred by plan)

### 2026-08-02 21:xx (caffeinate fix + panel AC verification)
- **Found + fixed a real production incident**: the 2026-08-02 05:00 scheduled run took 11+ hours (05:33→16:53) and ultimately failed to send — traced to the Mac sleeping mid-run (macOS suspends, doesn't kill, process execution on sleep; provider-call gaps of 60-90+ min are inconsistent with the code's own max-80s backoff, consistent with sleep/wake). Fixed by wrapping `ProgramArguments` in `caffeinate -i`, applied to both the repo's tracked plist and the installed `~/Library/LaunchAgents/` copy (preserved the panel-set 9:00 schedule, did not revert to the repo's stale 5:00). Verified via `launchctl start`: full run now completes in ~101s.
- **Found (separate, still open)**: Gmail API OAuth refresh token revoked (`invalid_grant`, first appeared in logs 2026-08-01) — both the primary (API) and fallback (SMTP/IMAP, stale app password) send paths are currently broken. Documented in README; fix requires human browser consent, cannot be automated from here.
- **Verified live**: AC2, AC6, AC7, AC8 for the control panel — all passed against the actual running :8787 instance (not just code review). See entries above.

## Housekeeping
- [ ] Branch: work is on `poc/noapi-google-search-mcp`; plans say implement on `feat/mcp-collector` — decide whether to rename/branch before PR to `main`
- [ ] Out-of-scope tracked elsewhere: 6am launchd DNS/proxy issue (`FIX_EMAIL_DELIVERY.md`)

## Work log (newest first — append a timestamped entry per session)

### 2026-07-25 20:40 (acceptance closure: AC5/AC7/AC8 + feed hygiene)
- **AC7 PASSED**: `scripts/check_style_marker.sh` exit 0 (marker in archive, style file restored). Fixed harness bug first: archive log line now has 3 comma-separated paths; sed took the whole list as one path.
- **AC5 PASSED**: Wi-Fi killed 8s into Collect (25s outage) → 26 per-source DNS soft-fails, no traceback, generated from DB (48 items), send correctly deduped (`already_sent`), exit 0. Wi-Fi restored via trap.
- **AC8 MET** (no-video): 73s and 61s full scheduled runs vs 5-min bar (briefing.log 19:05/20:13). With-transcription bound untested (no new video).
- **Feed hygiene**: check_feeds errors 8 → 1 (only Reddit r/ML 429, transient). Fixed URLs in subscriptions.json + live DB rows (feeds.db snapshot: data/feeds.db.bak-2026-07-25): PyTorch → pytorch.org/blog/feed.xml (10 items), LangChain → langchain.com/blog/rss.xml (100), Ahead of AI → magazine.sebastianraschka.com/feed (20), TLDR AI → tldr.tech/api/rss/ai (20). Retired (archived to subscriptions_retired.json, nothing deleted): AI News + MarkTechPost (WAF 403s urllib regardless of UA), The Batch (no working RSS endpoint found).

### 2026-07-25 20:13 (multi-recipient + full scheduled test through API path)
- **Second recipient added**: `RECIPIENT_EMAIL=juckrit@gmail.com,jsanitareephon@deloitte.com` (.env, comma-separated). Config now parses `RECIPIENT_EMAILS` list; all three transports updated (Gmail API + SMTP `sendmail` gets the list — a joined string would silently deliver only to the first; Outlook COM gets `;`-joined To).
- **Full scheduled launchd test at 20:13 PASSED**: Collect (5 new items) → Generate (6/6 Gemini) → Send both parts **via Gmail API (HTTPS/443)** — first scheduled run through the API-first path, log shows `sent via Gmail API` twice. Whole run ~61s.
- Schedule restored to 05:00, plist reloaded + verified. 79 tests green.

### 2026-07-25 ~20:00 (Gmail API primary + sender switch)
- **Sender identity switched to `jarkius.ai@gmail.com`** (`.env` GMAIL_ADDRESS; recipient stays juckrit@gmail.com; backup `.env.bak-2026-07-25`). Keeps OAuth token + app password off the personal account.
- **One-time OAuth done on this Mac** (user consent in browser): `data/gmail_oauth_client_secret.json` + `data/gmail_oauth_token.json` (both gitignored, per-machine). First 403 was the test-user gate — fixed by adding jarkius.ai as test user.
- **Gmail API (443) promoted to PRIMARY** in `sender.py`: `send_email` tries API→SMTP:465→SMTP:587→(win32) Outlook; `already_sent_today` tries API→IMAP, still fail-closed. Rationale: office network resets TLS on 465/587/993 but 443 always works — the API is the only transport that works everywhere.
- `require_env` no longer hard-requires GMAIL_APP_PASSWORD when the OAuth token exists (SMTP became the fallback).
- **Verified in anger**: real API-path send delivered (twice — direct `send_email_via_api` + full `send_email` ordering); real `already_sent_today` pre-check over API returns correctly. 78 tests green (was 54; sender tests rewritten for API-first, gmail_api tests added earlier).
- ⚠️ GMAIL_APP_PASSWORD in .env still belongs to juckrit@gmail.com — SMTP fallback auth will fail until a new app password for jarkius.ai is generated (optional; API is primary).

### 2026-07-25 19:05 (scheduled-run test)
- **Full scheduled run verified via launchd, email DELIVERED**: temporarily set StartCalendarInterval to 19:05, run fired on time — Collect (2 new items / 26 sources), Generate (6/6 Gemini calls, no quota errors), Send `{'part1': 'sent', 'part2': 'sent'}` in ~12s. Whole run 19:05:06→19:06:19 (~73s).
- Send succeeded without needing the Gmail API fallback this time (no SMTP-fallback lines in log) — fallback remains unverified-in-anger on the office network.
- Schedule restored to 05:00 and plist reloaded (verified via `plutil` + `launchctl list`).
- Note: this morning's 07:06 run had Generate fail with "Remote end closed connection without response"; tonight's run succeeded, so it was transient.

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
