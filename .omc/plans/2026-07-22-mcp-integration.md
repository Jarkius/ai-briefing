# Plan: Integrate noapi-google-search-mcp into ai-briefing

**Date:** 2026-07-22
**Branch:** `poc/noapi-google-search-mcp` → implementation on `feat/mcp-collector`
**POC evidence:** `docs/poc-noapi-google-search-mcp.md`; harnesses in `docs/poc_test_mcp_*.py`

## Requirements Summary

Replace ai_briefing's hand-rolled collectors (HN API, 11 RSS feeds, GitHub queries in `ai_briefing.py` ~lines 59–280) with the MCP feed layer (SQLite + FTS5), add YouTube transcription sources, and add two paste-driven workflows:

- **Research requests** — user pastes topics/URLs/YouTube links into `research_requests.md`; the pipeline researches them (visit_page / transcribe_video / search_feeds; google_search best-effort) and folds findings into the next newsletter.
- **Style refinement** — user pastes standing instructions into `newsletter_style.md`; the Gemini step reads it every run so the newsletter voice accumulates the user's taste.

Keep: Gemini summarization via maxplus (`ai_briefing.py:call_gemini`, key from `.env`), Gmail SMTP delivery (`ai_briefing.py` GMAIL section, ~lines 570–590), two-part email format (from `ai_briefing_v2.py`), launchd scheduling, `.md` archives as human-readable output.

**Constraints:**
- Two computers — all config/state that must sync lives in git; machine-local state (venv, SQLite DB, caches) is gitignored and rebuilt by a setup script.
- Google-branded MCP tools are bot-blocked from this network (POC) — must be optional enrichment with graceful skip, never a failure path.
- MCP requires Python 3.11 (rapidocr pin excludes 3.13+); system Python is 3.14 — project venv required.

## Architecture Decisions

1. **Vendoring:** Fork `VincentKaufmann/noapi-google-search-mcp` to `Jarkius/noapi-google-search-mcp`, commit the `noprogress` fix (2 sites in `server.py`, already patched in local clone), pin the fork by commit hash in `requirements.txt` / setup script. PR the fix upstream; switch back when merged.
2. **Client model:** The briefing orchestrator is an MCP **stdio client** (pattern proven in `docs/poc_test_mcp_stdio.py`): spawn server, call tools, exit. No long-running daemon.
3. **DB:** `FEEDS_DB_PATH=<repo>/data/feeds.db` (env var supported at `server.py:5386`). `data/` gitignored — DB is per-machine.
4. **Cross-machine send dedup:** SMTP has no idempotency, so send-tracking cannot live only in the per-machine DB. Before sending, `generator.py` does an IMAP pre-check (read-only, same account, method proven in this session): if a `AI Briefing Part 1 … <today>` message already exists, skip sending and log. `sent_items` in the local DB remains as the fast path; IMAP is the cross-machine source of truth.
5. **Declarative subscriptions:** `subscriptions.json` (committed) lists desired sources; orchestrator reconciles it against `list_subscriptions` each run — this is what syncs across computers, not the DB.
6. **One MCP server session per run:** `briefing.py` opens a single stdio session shared by collect + research phases (cold start loads Playwright/opencv/rapidocr once, ~5–10s observed in POC; whisper loads lazily on first transcription). Phases run strictly sequentially — collect → research both write via MCP tools, then the session is closed **before** `generator.py` opens its direct SQL connection. This sequencing is a stated invariant: no two writers ever hold the DB concurrently. Defensively, the generator connection still sets `PRAGMA busy_timeout=15000` and retries once, in case a killed subprocess left a WAL lock.
7. **New entrypoint** `briefing.py` (orchestrator); `ai_briefing.py` / `ai_briefing_v2.py` stay in git history and move to `legacy/` (nothing deleted).

## Implementation Steps

### Phase 1 — Vendor + environment
1. Fork upstream to `Jarkius/noapi-google-search-mcp`; push branch `fix/ytdlp-noprogress` with the patch from `~/workspace/lab/noapi-google-search-mcp` (both `ydl_opts` sites, `server.py` ~3648 and ~4023). Open upstream PR.
2. Add `setup.sh`: `uv venv --python 3.11 .venv` → `uv pip install git+https://github.com/Jarkius/noapi-google-search-mcp@<sha> mcp` → `.venv/bin/playwright install chromium`. Idempotent; run on each machine once.
3. Gitignore: `data/`, `.venv/`.

### Phase 2 — Collector (`collector.py`)
4. `subscriptions.json` initial content mirroring current sources: HN top, techcrunch + ars presets, custom RSS URLs for the remaining feeds in `ai_briefing.py:RSS_FEEDS`, GitHub repos of interest, arXiv `ai`+`ml`, and 2–3 YouTube AI channels (the beyond-text win).
5. `collector.py`: MCP stdio client that (a) reconciles subscriptions.json ↔ `list_subscriptions`, (b) runs `check_feeds` (auto-transcribes new YouTube videos), (c) logs counts per source.

### Phase 3 — Generator (`generator.py`)
6. Query `data/feeds.db` directly (schema at `server.py:5398`): items with `fetched_at` in last 24h, joined with a new `sent_items` table (ours, same DB file, separate table) for send-tracking. Guard against silent upstream schema drift with a startup check: `PRAGMA table_info(feed_items)` must contain the columns we read (`title,content,url,source_type,published_at,fetched_at`); on mismatch, abort with a clear "vendored server schema changed — review generator queries" error. (No version pragma exists upstream — verified; column check is the implementable guard.)
7. Build Gemini prompt with a hard input budget: **60,000 chars (~15k tokens) total item content**, allocated newest-first; each YouTube transcript capped at **8,000 chars** (head + tail, marker in between) before inclusion; if the total still exceeds budget, drop whole lowest-priority items (priority: research findings > HN/news > GitHub/arXiv > transcripts) rather than truncating mid-item. Output cap stays at the existing `max_tokens=4000` (`ai_briefing.py` Gemini call ~line 275). Append `newsletter_style.md` contents verbatim. Reuse `call_gemini` retry logic.
8. Reuse markdown→HTML + two-part split from `ai_briefing_v2.py:38–118`; write `.md` archive; send via existing SMTP code; mark items sent.

### Phase 4 — Research requests (`researcher.py`)
9. Parse `research_requests.md`: one request per `- [ ]` line (topic, URL, or YouTube link).
10. Route: YouTube URL → `transcribe_video`; other URL → `visit_page`; bare topic → `search_feeds` first, then `google_search`/`google_news` wrapped in try/skip (bot-block tolerated, noted in output).
11. Findings appended to a "Requested Research" newsletter section; request line flipped to `- [x]` with date (file stays in git — visible from both machines).

### Phase 5 — Wire-up
12. `briefing.py` = collect → research → generate → send, each phase failing soft (a phase failure logs and continues so a bot-block or one bad feed never kills delivery — the current pipeline's biggest weakness).
13. Update `com.user.ai-briefing.plist`: ProgramArguments → `<repo>/.venv/bin/python briefing.py`; keep 06:00 schedule and log paths; **add required env vars** — `HOME=/Users/jarkius` (Playwright cache `~/.cache/ms-playwright` and HuggingFace cache `~/.cache/huggingface` are HOME-relative and launchd does not guarantee HOME), plus the existing cleared-proxy vars. Acceptance for this step requires a run triggered via `launchctl start com.user.ai-briefing`, not a terminal run — the Jul 20–22 failures in `briefing.log` were launchd-environment-specific.
14. Move `ai_briefing.py`, `ai_briefing_v2.py` → `legacy/`; update README (setup.sh, new files, two-machine workflow).
15. Seed `newsletter_style.md` with the current format (two parts, sections, emoji headers) as the baseline the user then refines.

## Acceptance Criteria (all testable)

1. `./setup.sh` on a clean checkout produces a venv where `python -c "import google_search_mcp"` succeeds.
2. `python collector.py` exits 0; `sqlite3 data/feeds.db "SELECT COUNT(*) FROM feed_items"` increases; a subscribed YouTube channel's new video has a transcript row.
3. `python briefing.py` end-to-end: two emails arrive (verifiable via IMAP as done 2026-07-22), archive `.md` written, `sent_items` rows created; second run same day sends nothing (local dedup); a run with `sent_items` emptied but today's emails present in the mailbox also sends nothing (IMAP cross-machine dedup).
4. With `research_requests.md` containing one YouTube URL and one article URL: newsletter contains a "Requested Research" section citing both; checkboxes flipped.
5. With Wi-Fi disabled mid-collect: `briefing.py` still exits 0 and emails whatever the DB already holds (soft-fail proof).
6. `google_search` bot-block produces a logged skip, not a traceback (assert on POC-observed block response).
7. `newsletter_style.md` test: add rule "end the newsletter with the exact line STYLE-MARKER-42"; next `--dry-run` output contains `STYLE-MARKER-42` (scriptable string assert); remove rule afterward.
8. Wall-clock: full `briefing.py` run (no new YouTube videos) completes in < 5 minutes; with one new video to transcribe, < 15 minutes (tiny model). Measured and logged per phase.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Upstream repo changes/breaks | Pinned fork by SHA; we control upgrades |
| Google tools permanently blocked | Optional-enrichment design (step 12); core sources are RSS/HN/GitHub/arXiv/YouTube, all POC-verified |
| Whisper/Chromium footprint on both machines | `setup.sh` automates; models auto-download on first use |
| Two machines double-send | IMAP pre-check before send (architecture decision 4) — mailbox is the cross-machine source of truth; local `sent_items` is only a fast path |
| MCP server schema changes under our direct SQL | Pinned fork SHA makes schema stable; generator asserts expected columns via `PRAGMA table_info` at startup (step 6) |
| Lingering WAL lock from a killed MCP subprocess | Single-session sequencing invariant + `busy_timeout=15000` + one retry on the generator connection (architecture decision 6) |
| 6am DNS/proxy issue recurs (seen in `briefing.log` Jul 20–22) | Unresolved env issue, out of scope here, but soft-fail + DB means partial network still delivers a briefing |

## Verification Steps

1. `./setup.sh && .venv/bin/python -m pytest tests/` (unit: request parsing, prompt assembly, DB queries against a fixture DB).
2. `python briefing.py --dry-run` (new flag: full pipeline, print email instead of send).
3. Live run + IMAP check for the two emails (method used in this session).
4. Simulated-offline run for criterion 5.
5. `launchctl start com.user.ai-briefing` and inspect `briefing.log`.

## Out of Scope

- Fixing the 6am launchd DNS/proxy environment issue (tracked separately in `FIX_EMAIL_DELIVERY.md`).
- Feed-history analytics/weekly digests (enabled by the DB, planned later).
- Clip extraction / OCR ingestion (available in MCP, not wired in v1).

## Follow-ups

- [ ] When upstream merges the `noprogress` PR: repoint `setup.sh` from the fork SHA to an upstream release tag (tracked here; check monthly).

## Review Changelog

Critic review (2026-07-22) found 8 issues; all addressed:
1. False "SMTP idempotent" claim → replaced with IMAP pre-check design (decision 4) + AC3 cross-machine dedup test.
2. Non-existent schema-version assert → `PRAGMA table_info` column check (step 6, verified no version pragma upstream).
3. Vague token budget → hard numbers: 60k-char input budget, 8k-char/transcript cap, whole-item drop by priority (step 7).
4. Launchd env gap → HOME + cache-path env vars in plist; acceptance requires `launchctl start` trigger (step 13).
5. WAL concurrency unstated → single-session sequencing invariant + busy_timeout/retry (decision 6).
6. Unbounded subprocess cold-starts → one shared MCP session per run + wall-clock criterion (decision 6, AC8).
7. Untestable style criterion → STYLE-MARKER string assert (AC7).
8. Fork revert untracked → Follow-ups section.
