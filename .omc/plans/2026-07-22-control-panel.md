# Plan: Control Panel — Settings, Schedule, Manual Research, Preview, Manual Send

**Date:** 2026-07-22
**Branch:** `poc/noapi-google-search-mcp` → implementation on `feat/mcp-collector` (same branch as the collector plan)
**Depends on:** `.omc/plans/2026-07-22-mcp-integration.md` (this plan assumes `briefing.py`, `collector.py`, `generator.py`, `researcher.py`, `data/feeds.db`, `subscriptions.json`, `newsletter_style.md`, `research_requests.md` exist as designed there)

## Requirements Summary (from interview)

A local web dashboard sitting on top of the collector pipeline, so the user can preview the exact HTML email before/after sending, paste research requests and watch them run live, edit sources/style/schedule/credentials through forms instead of hand-editing files, and see recent run status/logs — without needing to open a terminal or an editor for day-to-day use.

**Decisions locked in interview:**

| Question | Decision |
|---|---|
| Form factor | Local web dashboard (not TUI/CLI-only) |
| Send flow | Auto-send stays as-is at 6am; dashboard is for extras (regenerate, resend, off-schedule send, send-after-research) |
| Research execution | Paste → runs immediately in the background with live progress, not queued |
| Server lifecycle | On-demand (`./panel.sh`), not an always-on LaunchAgent |
| Job durability | Background jobs (research, regenerate) survive tab close; only dying with the server process |
| Settings scope | Schedule time, sources list, style rules, **and credentials** (.env values) all editable in-app |
| Network exposure | Localhost-only (127.0.0.1) — required, since credentials are editable |
| Logs view | Live tail of current log + last-run-per-phase status badges (not full history table — that can come later) |
| Git sync | Dashboard saves auto-commit locally; push stays manual (same habit as code changes) |
| Frontend stack | Server-rendered Jinja + htmx, no build step, no node_modules |

## Architecture

### New dependency
- `fastapi`, `uvicorn`, `jinja2`, `python-multipart` (form posts) — small, stdlib-adjacent, no build tooling. Added to `setup.sh` pip install step from the collector plan.

### Process model
```
./panel.sh
    → uvicorn app:app --host 127.0.0.1 --port 8787   (foreground; Ctrl+C stops it)
    → opens http://127.0.0.1:8787 in default browser
    → app holds an in-process job registry (dict of job_id -> asyncio.Task)
        so research/regenerate jobs outlive any single browser tab
    → on Ctrl+C: running jobs are cancelled (documented behavior, not silent)
```
FastAPI app owns exactly one long-lived stdio session to the vendored MCP server (opened lazily on first tool call, closed on shutdown), so pasting three research requests in a row doesn't pay cold-start three times.

**Correction on session scope:** the mcp-integration plan's decision 6 ("one session, no two writers ever hold the DB concurrently") only guarantees sequencing *within a single `briefing.py` process invocation* — it does not, by itself, prevent the dashboard's independent long-lived session from overlapping with a separately-launched `briefing.py` cron run touching the same `feeds.db`. That is a real, distinct hazard this plan must solve on its own (see Cross-process lock below), not something already solved by citing the other plan.

### Cross-process lock (dashboard vs. cron `briefing.py`)
Both `briefing.py` (collect/research phases) and the dashboard's `/research/run` job make live MCP tool calls and write to `feeds.db`. A file lock at `data/.mcp.lock` (via `fcntl.flock`, non-blocking acquire) is held by whichever side is actively running MCP calls:
- `briefing.py` acquires the lock at the start of its collect phase and releases it after the research phase completes (before `generator.py` opens its direct SQL connection) — consistent with the collector plan's sequencing.
- The dashboard's `/research/run` job acquires the same lock before starting; if acquisition fails (cron is currently holding it), the job is rejected immediately with a clear "collection is running, try again in a minute" result rather than blocking or silently racing.
- If the dashboard holds the lock when cron fires, `briefing.py`'s collect phase retries acquisition with backoff for up to 2 minutes, then logs a soft-fail ("skipped collection — dashboard research in progress") and proceeds straight to generate/send from whatever `feeds.db` already holds — consistent with the collector plan's soft-fail philosophy (no crashed daily send).
- `/sources` and `/style`/`/schedule`/`/settings` never touch the lock — they don't make MCP calls (confirmed in their route descriptions below).

### Folder structure (modular separation, per request)
```
ai-briefing/
├── briefing.py                  # CLI entrypoint: collect/research/generate/send (collector plan)
├── panel.sh                     # dashboard launcher (this plan)
├── pyproject.toml               # single source of deps for both CLI + dashboard
├── setup.sh
├── src/
│   ├── briefing/                # core pipeline package (importable by both CLI and dashboard)
│   │   ├── __init__.py
│   │   ├── collector.py
│   │   ├── researcher.py
│   │   ├── generator.py
│   │   ├── sender.py            # SMTP + IMAP dedup check (extracted from ai_briefing.py)
│   │   ├── mcp_client.py        # shared MCP stdio session wrapper (used by collector + researcher + dashboard)
│   │   ├── db.py                # feeds.db access + schema guard (PRAGMA table_info check)
│   │   └── config.py            # .env loading, subscriptions.json, paths
│   └── panel/                   # dashboard package — depends on briefing, never the reverse
│       ├── __init__.py
│       ├── app.py               # FastAPI app + routes
│       ├── jobs.py              # background job registry (research/regenerate tasks)
│       ├── templates/
│       │   ├── base.html
│       │   ├── preview.html
│       │   ├── research.html
│       │   ├── style.html
│       │   ├── sources.html
│       │   ├── schedule.html
│       │   ├── settings.html
│       │   └── logs.html
│       └── static/
│           └── panel.css        # hand-written, not a framework — small surface area
├── data/                        # gitignored: feeds.db, run state
├── legacy/                      # ai_briefing.py, ai_briefing_v2.py (moved, not deleted)
├── subscriptions.json           # committed — source list
├── newsletter_style.md          # committed — style rules
├── research_requests.md         # committed — paste-in queue (used by CLI queued mode; dashboard bypasses when running live)
└── archives/                    # .md output, as today
```
`src/briefing/` has zero import of `src/panel/` — the CLI must keep working with no FastAPI installed if the user only wants the automated job. `panel.sh` fails fast with a clear message if dashboard deps are missing, rather than silently degrading.

### Routes (Jinja + htmx, no JSON API surface beyond htmx partials)

| Route | Method | Behavior |
|---|---|---|
| `/` | GET | Redirects to `/preview` |
| `/preview` | GET | Renders latest generated (or last-sent) newsletter HTML **inside an `<iframe srcdoc>`** — so the exact email HTML/CSS renders isolated from the dashboard's own styles |
| `/preview/regenerate` | POST | Enqueues a regenerate job (collect+research skipped, generate-only from current DB state); htmx polls `/jobs/{id}` and swaps the iframe when done |
| `/preview/send` | POST | Runs the IMAP-dedup-checked send; returns success/already-sent/error as an htmx swap banner |
| `/research` | GET | Textarea + list of past requests (parsed from `research_requests.md`, newest first) with status. If `JOBS` contains any non-terminal job (server-side, in-memory — see job model), renders its polling partial inline so a page reload/reopen reconnects to it; this is the only way AC7 (survives tab close) is achievable, since there is no per-job URL to bookmark |
| `/research/run` | POST | Acquires `data/.mcp.lock` (see Cross-process lock); on failure, returns immediately with "collection running, try again" and does not enqueue. On success, enqueues a live research job for the pasted text and returns an HTML fragment containing `<div hx-get="/jobs/{job_id}" hx-trigger="every 2s" hx-swap="outerHTML">` |
| `/jobs/{job_id}` | GET | Returns the current fragment for that job: phase text while running; on terminal state (done/error), returns a fragment with **no `hx-trigger`** (htmx's swap-in-response-without-the-attribute is how polling stops — the terminal fragment simply omits the polling attributes present in the running fragment) plus the finding/error and a "will appear in next preview" note |
| `/style` | GET/POST | Textarea bound to `newsletter_style.md`; POST writes file + local git commit |
| `/sources` | GET/POST | Table of `subscriptions.json` entries + add-source form (type + identifier); POST writes file + git commit; **does not** call `subscribe`/`unsubscribe` MCP tools directly — reconciliation happens on the next `collector.py` run, same as the collector plan's design, so the dashboard never opens a second MCP session concurrently with a running collect phase |
| `/schedule` | GET/POST | Time picker; POST rewrites `com.user.ai-briefing.plist`'s `StartCalendarInterval`, runs `launchctl unload`/`load`, commits the plist |
| `/settings` | GET/POST | Form for `.env` values (API key, Gmail address/password, recipient); POST rewrites `.env` (never committed — already gitignored); page displays a persistent warning banner: "this page is served on localhost only; do not port-forward or expose this machine's 8787 port" |
| `/logs` | GET | Tails last 200 lines of `briefing.log` (auto-refresh via `hx-trigger="every 3s"`) + a status strip of the 4 pipeline phases (collect/research/generate/send) with color/timestamp from the most recent run, read from a small `runs` table `generator.py`/`briefing.py` writes to (one row per run, one column per phase: `ok|error|skipped`, `started_at`, `duration_s`) |

### Job model (`src/panel/jobs.py`)
- In-memory dict `JOBS: dict[str, Job]`, `Job = {status, phase_text, result, error, started_at}`.
- `POST /research/run` calls `asyncio.create_task(...)`, stores it, returns `job_id`.
- Task holds the reused MCP session; on completion, writes findings into `feeds.db` (same schema as collector plan) and appends a `- [x]` line to `research_requests.md`, then runs `git commit -m "dashboard: research request completed" -- research_requests.md` (same pathspec-restricted pattern as steps 13–14 — never a bare commit).
- No persistence across server restart — acceptable per "on-demand, foreground" lifecycle decision; a job killed by Ctrl+C is simply lost and must be re-pasted, which is the documented tradeoff of choosing on-demand over always-on.

### Design direction for the UI itself
Dark, editorial-desk aesthetic — not a generic admin-panel template. Monospace/serif pairing (e.g. a distinctive serif for the nav/headers, a clean mono for logs and code-like content), warm dark background (near-black, not pure #000), one accent color reserved for "live/running" states and the send button. The `/preview` panel deliberately looks like a **letter on a desk** — bordered card with subtle paper texture — set apart from the dashboard's own dark chrome, so it's visually obvious "this rectangle is the email, everything outside it is the tool."

## Implementation Steps

### Phase 1 — Extract shared package (prerequisite refactor)
1. Create `src/briefing/` and move logic that both `briefing.py` CLI and the dashboard need: `.env` loading (`ai_briefing.py`'s config block, already refactored this session) → `config.py`; SMTP send + the new IMAP pre-check (collector plan decision 4) → `sender.py`; MCP stdio session open/close → `mcp_client.py`; feeds.db connection + schema guard → `db.py`.
2. `briefing.py` (collector plan's orchestrator) becomes a thin CLI over `src/briefing/*`.
3. Add a `runs` table to `feeds.db` (own migration in `db.py`, guarded like the `feed_items` check): `id, started_at, finished_at, source TEXT, collect_status, research_status, generate_status, send_status, error_text` where `source` is `'cron'` or `'dashboard'`. **Every write is an INSERT of a new row, never an UPDATE of an existing one** — `briefing.py` inserts one row per full run (its four phase columns filled in as it progresses, but it owns that row exclusively); a dashboard regenerate/send inserts its **own** row with only `generate_status`/`send_status` populated (`collect_status`/`research_status` left NULL, since those phases weren't run). This avoids two processes ever writing to the same row. `/logs`' status strip reads `ORDER BY started_at DESC LIMIT 1` per non-null phase column, not "the single latest row," so a dashboard-only send right after a cron collect still shows both correctly without either overwriting the other.

### Phase 2 — Dashboard skeleton
4. `pyproject.toml` add `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `httpx` (test client). `panel.sh`: activates `.venv`, runs `uvicorn src.panel.app:app --host 127.0.0.1 --port 8787 --reload` for dev / plain for normal use, opens browser via `python -m webbrowser`.
5. `src/panel/app.py`: FastAPI app, Jinja templates dir, static mount, startup event opens the shared MCP session (decision above), shutdown event closes it and cancels outstanding jobs with a logged warning.
6. `base.html`: nav (Preview/Research/Style/Sources/Schedule/Settings/Logs), status dot in header (green=idle, amber=job running, red=lock held by cron) fed by a new `GET /status` route (HTML fragment, `hx-trigger="every 3s"`) that checks `JOBS` for non-terminal entries and whether `data/.mcp.lock` is currently held.

### Phase 3 — Preview + manual send
7. `/preview`: the collector plan's `generator.py` (step 8) produces **two** HTML bodies (Part 1 / Part 2, per the existing `ai_briefing_v2.py:38–118` split this plan inherits). `/preview` shows both as two stacked `<iframe srcdoc>` blocks labeled "Part 1: News & Learning" / "Part 2: Technical & Community", each reading the exact same in-memory HTML string that `sender.py` would hand to SMTP for that part — the generator function used for this page is whatever `generator.py` exposes as its markdown→HTML step (module/function name to be fixed when that file is written per the collector plan; this plan requires it be a plain string-returning function importable without side effects, so `/preview` can call it without re-sending anything).
8. `/preview/regenerate`: calls `generator.py`'s generate function directly (in-process, not a subprocess) against current DB state; updates `runs` row; htmx swap.
9. `/preview/send`: calls `sender.py`'s IMAP-checked send; on "already sent today" returns a distinct banner (not an error) so manual re-send attempts are self-explanatory.

### Phase 4 — Research (live)
10. `/research` GET: parse `research_requests.md` checkbox lines into a simple list for display (reuse the collector plan's parser from `researcher.py`).
11. `/research/run` POST: enqueue job per the job model above; template partial polls and renders phase text, then final findings snippet + "will appear in next preview" note.

### Phase 5 — Sources, Style, Schedule, Settings, Logs
12. `/sources`: read/write `subscriptions.json`; add-source form validates `source_type` against the collector plan's known types before writing (no free-text type field, prevents typos silently breaking the next `check_feeds`).
13. `/style`: textarea bound to `newsletter_style.md`, save = write file, then `git commit -m "dashboard: update newsletter style" -- newsletter_style.md` (pathspec-restricted; **never** a bare `git commit` or `add -A`, so a manual `git add` the user has staged in a terminal is never swept into this commit). Errors surfaced (e.g. nothing to commit, or a dirty index conflict on that exact path) as a non-blocking banner — the file write itself always succeeds first.
14. `/schedule`: time picker → rewrite `<key>Hour</key>/<key>Minute</key>` in `com.user.ai-briefing.plist` via `plistlib` (not string templating, to avoid XML corruption) → `launchctl unload` + `load` (subprocess, capture stderr) → `git commit -m "dashboard: update schedule" -- com.user.ai-briefing.plist` (same pathspec-restricted pattern as step 13).
15. `/settings`: form for `.env` keys; **explicit non-goal**: no masking/obfuscation theater (localhost-only already agreed as the mitigation) but the page must show a static warning line (per design direction) and never log the values server-side.
16. `/logs`: tail `briefing.log`; status strip reads the latest `runs` row.

### Phase 6 — Polish + docs
17. `panel.css`: dark editorial theme per Design Direction above — implemented directly as hand-written CSS, no framework.
18. README: add "Dashboard" section — `./panel.sh`, what each tab does, the localhost-only/credentials warning restated for anyone reading later.
19. `.gitignore`: no new entries needed (`data/`, `.venv/` already covered by collector plan); confirm `__pycache__/` for new `src/` tree is covered by existing `*.py[cod]` rule.

## Acceptance Criteria (all testable)

1. `./setup.sh && ./panel.sh` on a clean checkout starts the server and opens a browser tab reachable at `http://127.0.0.1:8787/preview` within 10s.
2. `curl http://0.0.0.0:8787/preview` from another machine on the LAN fails to connect (proves localhost-only binding).
3. `/preview` renders both parts using the exact same in-memory HTML strings `sender.py` passes to SMTP for that run — proven by a unit test that calls the generator function once, asserts the `/preview` route's rendered fragment for each part equals that string exactly (no second render, no reformatting), rather than diffing against IMAP-fetched mail (MIME transfer encoding makes a raw byte-diff against fetched content unreliable and is not attempted).
4. Clicking Regenerate updates the `runs` table row and the iframe content changes if DB content changed since generation; if unchanged, banner states "no new items since last generation."
5. Clicking Send when today's email already exists in the mailbox (IMAP check) shows "already sent today" and does **not** place a second email in Sent/Inbox (IMAP-verified before/after count).
6. Pasting a YouTube URL into `/research` and submitting: within 2s the UI shows a running state; polling shows phase text; on completion (< 15 min per collector plan's wall-clock criterion) the finding appears, `research_requests.md` shows the line flipped to `- [x]`, and one local git commit exists for that file.
7. Closing the browser tab mid-research-job, then reopening `/research`, shows the job still running or completed (job survived tab close) — proven by killing the tab process, waiting, reopening.
8. Killing the server process (Ctrl+C) mid-job, then checking `research_requests.md`: the line is **not** flipped (job died with the process, as documented) — no partial/corrupt state.
9. Editing and saving `/style` produces exactly one new git commit with `newsletter_style.md` in its diff; `git log` shows no auto-push (`git status` on the remote-tracking branch shows local-ahead, confirmed via `git log origin/<branch>..HEAD`).
10. Changing `/schedule` to a new time, saving, then `launchctl list | grep ai-briefing` and `plutil -p ~/Library/LaunchAgents/com.user.ai-briefing.plist` both reflect the new hour/minute within the same request (no manual reload needed).
11. Editing `.env` values via `/settings`, saving, then a fresh `briefing.py` run picks up the new value (proves the form writes the actual `.env` the CLI reads, not a shadow copy) — and `git status` shows `.env` untouched/ignored.
12. `/logs` shows the tail of `briefing.log` updating live (new line appended to the file while the tab is open causes a visible update within 3s) and the 4-phase status strip matches the latest `runs` row exactly.
13. `src/briefing/` imports cleanly and `briefing.py collect` runs successfully in a venv where FastAPI/uvicorn/jinja2 are **not installed** (proves CLI/dashboard are genuinely decoupled per the folder-structure invariant).

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Credentials editable through a local web UI | Localhost-only binding (AC2) is the primary control; explicit on-page warning (step 15); no remote-access feature exists to disable later — it was never built |
| Dashboard's long-lived MCP session collides with a separately-launched cron `briefing.py` run (two independent processes, each with their own session, touching `feeds.db` at the same time) | `data/.mcp.lock` file lock (see Cross-process lock section) — whichever side can't acquire it either rejects immediately (dashboard) or soft-fails that phase with backoff (cron), never silently races |
| `/sources` writes racing a concurrent `check_feeds` read in `collector.py` | Not applicable — `/sources` only edits `subscriptions.json`, never opens the MCP session or touches `feeds.db` directly |
| Background research job silently dies (uncaught exception in the asyncio task) | Task wrapped in try/except that writes `Job.status = "error"` + `Job.error = str(e)`; UI polling surfaces errors, never hangs on "running" forever |
| `git commit` from the dashboard fails (e.g. merge conflict, detached HEAD from prior manual work) | Every dashboard git call captures stderr and surfaces as a banner rather than a 500; the file write itself always succeeds first, so a failed commit never loses the user's edit — worst case, they commit manually later |
| Plist rewrite corrupts XML | Use `plistlib.load`/`dump` (structured), never string replace, for step 14 |
| iframe content-security or font loading differs from real email clients | Explicitly out of scope (see below) — iframe proves HTML/CSS correctness, not every email client's rendering quirks |
| Two computers both run `./panel.sh` at once pointing at the same repo's `data/feeds.db` | Out of scope for v1 (documented single-user-at-a-time assumption); `db.py`'s `busy_timeout` from the collector plan provides some cushion but this plan does not add multi-writer locking beyond that |

## Out of Scope

- Full run-history table (deferred; `runs` table schema chosen to make this easy later).
- Cross-client email rendering testing (Outlook/Gmail-app quirks) — iframe preview proves our HTML, not every renderer.
- Auto-push on save (explicitly rejected in interview — manual push preserved).
- Remote/LAN access, auth/login screens (localhost-only chosen explicitly).
- Job persistence across server restarts (documented tradeoff of on-demand lifecycle).
- Editing `subscriptions.json` triggering live `subscribe`/`unsubscribe` calls (reconciliation deferred to next collector run, per concurrency mitigation above).

## Verification Steps

1. `./setup.sh && ./panel.sh`, manual click-through of all 7 tabs.
2. `pytest tests/panel/` — httpx `TestClient` against each route (form submit → file/DB state assertion), covering AC1, 4, 6, 8, 9, 10, 11, 13.
3. Unit test (string-identity assertion, no IMAP) for AC3; IMAP-based before/after count check for AC5 (method proven earlier this session).
4. Manual kill-tab and kill-server tests for AC7/AC8.
5. `curl` from a second machine on the LAN for AC2.

## ADR

**Decision:** Build a localhost-only, on-demand FastAPI + Jinja/htmx dashboard as a `src/panel/` package layered strictly on top of a `src/briefing/` core package that the existing CLI also uses — rather than a TUI, a CLI-only tool, an always-on service, or a React SPA.

**Drivers:** (1) real HTML email preview requires a browser-renderable surface, ruling out TUI/CLI-only; (2) credentials become editable, ruling out LAN/remote exposure; (3) solo maintenance across two computers favors zero build-tooling (htmx over React) and on-demand over always-on (fewer background processes to keep healthy on two machines).

**Alternatives considered:**
- *TUI (Textual)* — rejected: cannot render real HTML, defeats the primary "preview" requirement.
- *CLI + static preview.html* — rejected: no forms for settings/sources/schedule, paste-to-research has no live-progress surface.
- *Always-on LaunchAgent dashboard* — rejected: orphaned-job recovery complexity across sleep/restart not justified for a personal tool used briefly per day.
- *React + Vite SPA* — rejected: adds a second toolchain (node_modules, build step) to keep in sync on two machines, for a UI whose interactivity (polling, form posts, iframe swap) htmx covers natively.
- *LAN-reachable with shared-secret auth* — rejected: user explicitly chose localhost-only once credential-editing was named as the driver; can be revisited if multi-machine dashboard access becomes a real need.

**Why chosen:** Matches every explicit interview decision, keeps the CLI usable without the dashboard's dependencies (AC13), and reuses the collector plan's schema-guard invariant while adding this plan's own cross-process file lock to close the concurrency gap the companion plan's single-run sequencing does not cover.

**Consequences:** Settings screen carries real risk (plaintext credentials in a local web form) mitigated only by network binding — accepted explicitly, not silently. On-demand lifecycle means a crashed dashboard loses in-flight research jobs — accepted explicitly as simpler than persistence. The file lock adds one more piece of cross-process state (`data/.mcp.lock`) that must be cleaned up correctly on crash (accepted: `fcntl.flock` releases automatically on process exit, including crash, so no manual cleanup step is needed).

**Follow-ups:**
- [ ] If multi-machine remote dashboard access becomes wanted later, revisit LAN+auth option — do not silently open the bind address without adding auth in the same change.
- [ ] If research jobs become long/valuable enough that losing them to a crash hurts, add job persistence (a `jobs` table) — not needed at current scope.
- [ ] When `generator.py` is actually written (collector plan), confirm its markdown→HTML step is a plain importable function per step 7's requirement here — flag back to this plan if the collector plan's implementation makes that impractical.

## Review Changelog

Critic review (2026-07-22) found 8 issues; all addressed:
1. Bare `git commit` would sweep in unrelated staged changes → all three dashboard commit sites (style, schedule, research) now use pathspec-restricted commits (`-- <file>`), never `add -A`/bare commit.
2. "Two MCP sessions collide" mitigation only covered `/sources` (which never touches MCP) → added the actual `data/.mcp.lock` cross-process lock covering `/research/run` vs. cron `briefing.py`.
3. Plan overstated what the collector plan's decision 6 actually guarantees (single-run sequencing, not cross-process exclusion) → corrected with an explicit "Correction on session scope" note; the lock is this plan's own addition, not borrowed safety.
4. No mitigation existed for concurrent `runs`-table writes from cron vs. dashboard → redesigned as insert-only (never update-in-place), `source` column distinguishes writers, `/logs` reads per-phase latest rather than assuming one row.
5. `/jobs/{id}` and `/status` were referenced but never defined → both added to the Routes table with method, htmx wiring, and how polling starts/stops.
6. AC7 (job survives tab close) was unimplementable — no way for a fresh page load to discover the job → `/research` GET now checks in-memory `JOBS` for non-terminal entries and re-renders the polling fragment automatically.
7. AC3's byte-diff against IMAP-fetched mail was unreliable (MIME encoding) and ignored the two-part email format → replaced with a same-string-identity unit test against each part's in-memory HTML, and `/preview` now explicitly shows two iframes.
8. Step 7 cited a specific `generator.py` function that doesn't exist yet in the companion (unwritten) file → loosened to a stated contract (plain importable string-returning function) with a follow-up to reconcile once that file is written.
