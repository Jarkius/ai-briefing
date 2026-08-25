# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily AI news briefing pipeline: collects from a zero-API-key MCP feed
layer (RSS, HackerNews, arXiv, YouTube with auto-transcription), summarizes
with an LLM, and delivers as a two-part HTML email. Runs on a schedule
(launchd on macOS, Task Scheduler on Windows) and has an optional local web
dashboard for manual control. This repo is worked on from two machines —
see "Two-computer workflow" below.

## Commands

```bash
./setup.sh                              # one-time: .venv (Python 3.11) + vendored MCP fork + Playwright Chromium
.venv/bin/python run.py                 # full pipeline run, sends email
.venv/bin/python run.py --dry-run       # full pipeline run, prints instead of sending
./panel.sh                              # local dashboard at http://127.0.0.1:8787
.venv/bin/python -m pytest              # run all tests
.venv/bin/python -m pytest tests/test_generator.py::test_name   # single test
.venv/bin/python -m pytest tests/panel/  # panel-only tests
.venv/bin/python scripts/setup_gmail_oauth.py   # one-time Gmail API OAuth setup (Mac/Linux fallback transport)
./scripts/check_style_marker.sh         # proves a newsletter_style.md edit actually reaches the LLM prompt (Mac/Linux; real --dry-run, restores the file after)
```

There is no lint/format tooling configured (no ruff/black in pyproject.toml)
and no CI workflow — tests are run locally. `pytest` requires `pip install
-e ".[dev,panel]"` (or the full `setup.sh`) for panel tests to import.

Windows has no `setup.sh` equivalent; per README.md, build the venv manually
(`py -3.11 -m venv .venv && .venv\Scripts\pip install -e .`) and use
`setup_task.bat` / `run_briefing.bat` for scheduling instead of launchd.

## Architecture

```
subscriptions.json ──▶ collector.py ──▶ data/feeds.db (SQLite + FTS5)
                              │               ▲
research_requests.md ──▶ researcher.py ───────┘
                                                │
newsletter_style.md ──▶ generator.py ◀─────────┘
                              │
                              ▼
                    sender.py (SMTP + IMAP dedup)
                              │
                              ▼
                    archives/*.md + 2 emails
```

`run.py` orchestrates collect → research → generate → send → social-post as
five independently-soft-failing phases (`src/briefing/*.py`); a broken feed
or a lock contention with a concurrent dashboard job never aborts the whole
run — each phase logs and the pipeline continues with whatever data it has.
Per-phase status is written to the `runs` table (`db.py`) after every phase,
not just at the end.

- **`collector.py`** reconciles `subscriptions.json` against the vendored MCP
  server's subscription table (matched by `name`, not the stored
  `identifier` — the server rewrites identifiers for some source types after
  a network lookup), then runs `check_feeds`. YouTube auto-transcription is
  capped (2 videos / 30 min each per run) so a backlog can't block a daily
  run.
- **`researcher.py`** processes `research_requests.md` (one topic/URL per
  line), routing to `transcribe_video` / `visit_page` / `search_feeds` +
  best-effort `google_search`. Findings persist to `research_store.py`
  (`data/research_tasks.db`) — a narrow durable store added specifically to
  fix silent findings loss; see "Deferred: workflow state machine" below.
- **`generator.py`** budgets recent DB items into an LLM prompt (hard
  60k-char cap, 8k-char/transcript cap, priority-based dropping), appends
  `newsletter_style.md` verbatim, and tries providers in order via
  `PROVIDER_ORDER` (default `bedrock,gemini,maxplus,claude-cli` — Bedrock
  Claude, Gemini direct, Gemini-via-maxplus, and a `claude` CLI subprocess as
  a last-resort tier with no separate API quota).
- **`sender.py`** sends via Gmail API over HTTPS (primary, once OAuth is set
  up — survives networks that reset SMTP/IMAP TLS) or SMTP (fallback; sole
  path without OAuth), with an IMAP pre-check so a second run/machine never
  double-sends the same day's briefing. Windows has no Gmail-API fallback;
  its fallback chain is SMTP → Outlook COM automation instead.
- **`mcp_client.py`** wraps the vendored `noapi-google-search-mcp` stdio
  server (one subprocess/session per pipeline invocation, not per tool
  call) and owns `data/.mcp.lock`, a cross-process advisory lock so a cron
  run and the dashboard's long-lived session never touch `feeds.db`
  concurrently. `fcntl` on Unix, `msvcrt` on Windows.
- **`db.py`** treats `feed_items`/`subscriptions`/`feed_items_fts` as owned
  by the vendored MCP server (read-only, with a schema-drift guard) and owns
  only the `runs` table itself. Also owns `data/send_status.json` (guarded
  by a separate `send_lock()`) recording per-archive delivery outcome.
- **`src/panel/`** (`app.py`, `jobs.py`, `state.py`) is a FastAPI dashboard
  reusing the same pipeline modules directly — Preview/Regenerate calls
  `generator.generate()` in-memory (no second render path), Send calls the
  same dedup-checked `sender.py` path as cron. Research jobs run as
  background tasks that survive tab close (die only with the server).
  Binds to `127.0.0.1` only, by design — `/settings` writes real credentials
  and there is no auth screen; localhost-only *is* the access control.

### Config loading (`config.py`)

`.env` is parsed by a hand-rolled loader (no external deps) that
**overrides ambient shell exports** for every key it reads — several names
(`MAXPLUS_API_KEY` especially) are also used by unrelated dev tooling and
can be exported globally in a shell, and `.env` must win so that
commenting out a key there actually disables it. `config.reload()`
re-reads `.env` for the panel's long-lived process after a `/settings` edit;
CLI runs never need it. Module constants (`config.MAXPLUS_API_KEY` etc.) are
rebound by `reload()`, so always access them via `config.X`, never via a
`from briefing.config import X` snapshot import.

### Concurrency model

Two writers can run at once: a scheduled cron/launchd `run.py` and a
manually-started dashboard job. Three independent locks coordinate them,
each scoped to exactly the resource it protects — don't conflate them:

- `data/.mcp.lock` (`mcp_client.mcp_lock()`) — the MCP stdio session/`feeds.db`
  writes. Dashboard jobs use `retry_seconds=0` (fail fast, never block a
  user-facing request); cron retries with backoff (tolerate a brief
  dashboard hold rather than failing the whole daily run).
- `data/.send.lock` (`db.send_lock()`) — the dedup-check-then-send sequence
  and `send_status.json` read-modify-write.
- SQLite's own `busy_timeout` — the `runs` table (`db.py`) can transiently
  lock against the vendored server's `feed_items` writes in the same file;
  `_retry_locked()` degrades to a log line rather than killing the pipeline
  over a status-tracking write.

### Deferred: workflow state machine (see `docs/adr/0001-*.md`)

A prior branch (PR #7/#12) proposed replacing this scattered per-subsystem
state (`runs` table, `send_status.json`, `panel/state.py` in-memory globals,
`research_requests.md` as a markdown queue) with one `workflow.py` +
`data/workflow.db` command path shared by cron and the panel. It was closed
without merging — real foundation work, but never wired into `run.py` or
`app.py`, and it conflicted with `research_store.py` landing the same week
as a narrower fix for the same durability gap. Read the ADR before
resurrecting this idea; it documents concrete revisit triggers.

## Two-computer workflow

This repo is worked on from two machines. Always work on a feature branch,
never commit directly to `main` — open a PR. `data/` (feeds.db,
research_tasks.db, the Gmail OAuth token) and `.venv/` are gitignored
per-machine state; `subscriptions.json`, `newsletter_style.md`, and
`research_requests.md` are the git-tracked files that sync configuration
and in-flight research between machines.

## Other things to know

- `legacy/ai_briefing.py` / `ai_briefing_v2.py` are the pre-MCP single-file
  implementations, kept for reference only — not part of the running
  pipeline.
- `src/briefing/config.restrict_to_owner_only()` is a reminder that
  `os.chmod(0o600)` is a no-op for real access control on Windows/NTFS; the
  Windows path uses `icacls` instead.
- Secrets (`.env`, the Gmail OAuth token in `data/`) are gitignored — never
  commit them, and don't widen `mcp_client._server_params()`'s minimal env
  allowlist to pass credentials into the vendored third-party MCP subprocess.
