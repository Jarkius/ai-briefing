# Daily AI Briefing

Automated daily AI briefing: collects from a zero-API-key MCP feed layer
(RSS, HackerNews, arXiv, YouTube — with automatic transcription), summarizes
with Gemini (via maxplus), and delivers as a two-part HTML email.

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

- **`collector.py`** — reconciles `subscriptions.json` against the vendored
  MCP server's subscription table, then runs `check_feeds` (auto-transcribes
  new YouTube videos, capped at 2 videos / 30 min each per run so a big
  backlog can't block a daily run).
- **`researcher.py`** — processes pasted requests in `research_requests.md`
  (a topic, article URL, or YouTube link per line), routing to
  `transcribe_video` / `visit_page` / `search_feeds` + best-effort
  `google_search`.
- **`generator.py`** — budgets recent DB items into a Gemini prompt (hard
  60k-char cap, 8k-char/transcript cap, priority-based dropping), appends
  `newsletter_style.md` verbatim, produces two HTML bodies.
- **`sender.py`** — SMTP send with an IMAP pre-check so a second run (or a
  second computer) never double-sends the same day's briefing.
- **`run.py`** — orchestrates collect → research → generate → send, each
  phase failing soft so one broken source never blocks delivery.

See `.omc/plans/2026-07-22-mcp-integration.md` for the full design.

## Setup

### Prerequisites

- Python 3.11 or 3.12 (the vendored MCP server pins `rapidocr-onnxruntime<3.13`)
- [`uv`](https://docs.astral.sh/uv/)
- Gmail account with an App Password
- A maxplus-ai.cc (or compatible) API key

### Install

```bash
./setup.sh
```

Builds `.venv` (Python 3.11), installs this project plus the vendored
`noapi-google-search-mcp` fork (pinned by commit SHA — see `setup.sh`), and
installs Playwright's Chromium.

### Configuration

```bash
cp .env.example .env
```

```dotenv
GEMINI_API_KEY=your-google-gemini-api-key   # optional fallback #1
MAXPLUS_API_KEY=ccsk-your-api-key-here
MAXPLUS_MODEL=gemini-3.5-flash        # check your maxplus pool's available models
GMAIL_ADDRESS=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password
RECIPIENT_EMAIL=recipient@gmail.com   # optional, defaults to GMAIL_ADDRESS
```

`.env` is gitignored — never commit real credentials.

### Sources

Edit `subscriptions.json` to add/remove feeds. Supported `source_type`
values: `news`, `reddit`, `hackernews`, `github`, `arxiv`, `youtube`,
`podcast`, `twitter`. See the vendored server's `subscribe` tool docstring
for identifier formats per type.

## Usage

```bash
.venv/bin/python run.py              # full run, sends email
.venv/bin/python run.py --dry-run    # full run, prints instead of sending
```

### Pasting research requests

Add a line to `research_requests.md`:

```markdown
- [ ] https://www.youtube.com/watch?v=...
- [ ] https://some-article.example.com/post
- [ ] Kimi K3 benchmark results
```

The next `run.py` invocation researches each and folds findings into the
newsletter, checking off completed lines with a date.

### Refining newsletter style

Edit `newsletter_style.md` — its contents are appended verbatim to every
Gemini prompt, so style changes take effect on the next run without touching
code.

### Scheduling (launchd, macOS)

```bash
cp com.user.ai-briefing.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.ai-briefing.plist
```

Runs daily at 06:00. Logs to `briefing.log` / `briefing_error.log`.

### Scheduled Run (Windows)

Use Windows Task Scheduler with the included wrapper scripts:

1. Copy `.env.example` to `.env` and fill in your credentials (see Configuration above).
2. Create the venv and install the project (there's no Windows equivalent of `setup.sh`
   yet — run these manually from a `cmd`/PowerShell prompt in the repo root):
   ```bat
   py -3.11 -m venv .venv
   .venv\Scripts\pip install -e .
   ```
   (The vendored `noapi-google-search-mcp` fork and Playwright's Chromium — see
   `setup.sh` — are only required for the collector/researcher phases; install them
   the same way if you run those on Windows too.)
3. Right-click `setup_task.bat` → **Run as administrator**. This registers a daily task
   (`\ai\AI Briefing Daily`) that runs `run_briefing.bat`, which in turn calls
   `.venv\Scripts\python.exe run.py` and logs output to `logs\briefing_YYYY-MM-DD.log`.
4. If the task needs to survive screen lock/logoff, or run on battery, use
   `fix_task_settings.ps1` (run from an elevated PowerShell prompt) to switch the task's
   power/logon settings.

**Email delivery on corporate networks:** the current pipeline (`src/briefing/sender.py`)
only sends via Gmail SMTP (trying port 465, then falling back to 587 — see
"Gmail SMTP errors" below). The legacy `ai_briefing.py` had an Outlook COM automation
fallback for networks where a proxy/DLP agent (e.g. Netskope) blocks outbound SMTP; that
fallback has not been ported to the new pipeline. If you're on such a network, outbound
Gmail SMTP must be reachable for scheduled sends to succeed.

## Two-computer workflow

This repo is used from two Macs. Always work on a feature branch, never
commit directly to `main`. `data/` (the local feeds DB) and `.venv/` are
gitignored per-machine state — `subscriptions.json`, `newsletter_style.md`,
and `research_requests.md` are the git-tracked files that sync your
configuration and in-flight research between machines.

## Legacy

`legacy/ai_briefing.py` and `legacy/ai_briefing_v2.py` are the original
single-file implementations (hand-rolled fetchers, no database), kept for
reference — nothing is deleted. See `docs/poc-noapi-google-search-mcp.md`
for why the MCP-based collector replaced them.

## Gmail API fallback (networks that block SMTP/IMAP)

Some networks only allow outbound HTTPS (port 443) and block the raw
SMTP/IMAP protocol ports (465/587/993) — confirmed on at least one office
network this project runs on: TLS handshakes to `smtp.gmail.com` /
`imap.gmail.com` reset consistently there, while HTTPS to Google works fine.

`sender.py` tries SMTP first (no setup needed, works on most networks) and
automatically falls back to the Gmail API over HTTPS if SMTP fails and the
fallback is configured. Setup is one-time and needs a real browser login
(this can't be scripted around — Google requires an explicit consent click):

```bash
.venv/bin/python scripts/setup_gmail_oauth.py
```

The script prints exact URLs for creating a Google Cloud project + OAuth
"Desktop app" credentials, then opens a browser for the consent screen and
saves a refresh token to `data/gmail_oauth_token.json` (gitignored,
per-machine — never commit it). After that, `run.py` uses it automatically
whenever SMTP fails; no `.env` changes needed.

On Windows, the fallback chain is SMTP → Outlook COM automation instead
(requires a local Outlook install) — the Gmail API fallback only applies on
macOS/Linux where there's no Outlook to fall back to.

## Troubleshooting

### Gemini returns HTTP 400 "model not available"
The maxplus pool's available models change over time. The 400 response body
lists currently valid model names — update `MAXPLUS_MODEL` in `.env`.
Likewise, `gemini-3.5-flash` was never a real Gemini model name (verified
against the API's own model list) — use `GEMINI_MODEL=gemini-flash-latest`
or another name from `https://generativelanguage.googleapis.com/v1beta/models`.

### Gmail SMTP/IMAP errors, or "Connection reset by peer"
Enable 2FA on the Gmail account and use an App Password, not the regular
password. If the error is specifically a connection reset (not an auth
failure) and persists across retries, the network is likely blocking the
SMTP/IMAP protocol ports — see "Gmail API fallback" above.

### A leftover shell environment variable overrides `.env`
`MAXPLUS_API_KEY` in particular is also used by unrelated dev tooling (e.g.
some Claude Code shell configs export it globally). `config.py`'s `.env`
loader overrides ambient shell exports for every key it reads, so a value
set in `.env` (including intentionally commenting a key out) always wins —
but this only applies to processes that actually load `.env` via
`config.py`; a raw `echo $MAXPLUS_API_KEY` in your shell will still show
whatever's exported there, which is expected and harmless.

### `data/.mcp.lock` seems stuck
The lock (`fcntl.flock`) releases automatically when its holding process
exits, including on crash or kill — it cannot outlive the process that took
it. If `run.py` reports the collector was skipped due to the lock, another
process (a manual `collector.py`/`researcher.py` run, or the dashboard) was
using the MCP server at that moment; the run still completes using whatever
was already in `data/feeds.db`.
