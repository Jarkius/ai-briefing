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

One entry point regardless of where secrets come from — `config.py` pulls
from Bitwarden Secrets Manager internally if this machine has done that
one-time setup (see "Secrets Manager" below), falling back to `.env`
otherwise. Nothing else to remember or run differently.

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

Runs daily at 05:00. Logs to `briefing.log` / `briefing_error.log`. The
plist calls `scripts/run_with_secrets.sh`, not `run.py` directly — see
"Secrets Manager (Bitwarden, optional)" above; it works with plain `.env`
too if Bitwarden isn't set up on that machine.

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

**Email delivery on corporate networks:** once the one-time OAuth setup is done
(see "Gmail API fallback" below), `src/briefing/sender.py` sends via the **Gmail API
over HTTPS/443 as the primary transport** — this works even on networks that reset
TLS on the SMTP/IMAP ports. SMTP (465, then 587) is the fallback, and on Windows a
further Outlook COM fallback exists. Without the OAuth token, SMTP is the only path
and outbound Gmail SMTP must be reachable for scheduled sends to succeed.

## Dashboard (control panel)

An on-demand local web UI over the same pipeline:

```bash
./panel.sh        # serves http://127.0.0.1:8787 and opens your browser
```

| Tab | What it does |
|---|---|
| **Preview** | Renders both email parts exactly as they'd be sent (same in-memory HTML — no second render path). Regenerate re-runs the generate step from current DB state; Send does the dedup-checked real send. |
| **Research** | Paste YouTube URLs / article URLs / topics; runs immediately in the background with live phase text. Jobs survive tab close (they die only with the server). |
| **Style** | Edit `newsletter_style.md`; saves auto-commit locally (push stays manual). |
| **Sources** | View/add `subscriptions.json` entries; new sources subscribe on the next collect run. |
| **Schedule** | Change the daily launchd run time; rewrites the plist and reloads it immediately. |
| **Settings** | Edit `.env` values; applied to the running server immediately, never committed. |
| **Logs** | Live tail of `briefing.log` (cron runs), dashboard-job list, and a per-phase status strip. |

**Security note (do not skip):** `/settings` edits real credentials, so the panel
binds to `127.0.0.1` only. Never port-forward or expose port 8787 — there is no
auth screen, by design; localhost-only *is* the access control. The CLI pipeline
keeps working without the dashboard's dependencies installed.

## Two-computer workflow

This repo is used from two Macs. Always work on a feature branch, never
commit directly to `main`. `data/` (the local feeds DB) and `.venv/` are
gitignored per-machine state — `subscriptions.json`, `newsletter_style.md`,
and `research_requests.md` are the git-tracked files that sync your
configuration and in-flight research between machines. Secrets sync via
Bitwarden Secrets Manager instead of manually copying `.env` — see below.

## Secrets Manager (Bitwarden, optional)

`.env` still works standalone (see Configuration above) — this is an
additive layer for running the same secrets across multiple machines
without ever copy-pasting `.env` between them again.

**One-time setup (per Bitwarden organization, not per machine):**
1. In a Bitwarden organization, create a Secrets Manager project (this repo
   uses one named `ai-briefing`).
2. Add each `.env` key you actually use as a secret in that project (skip
   ones you don't use — e.g. no point syncing a disabled provider's key).

**Per-machine setup:**
1. Install the `bws` CLI — no Homebrew formula; download the release for
   your platform from https://github.com/bitwarden/sdk-sm/releases (assets
   tagged `bws-vX.Y.Z`) and put the binary on `PATH` (e.g. `~/.local/bin`).
2. In the Bitwarden org → Secrets Manager → **Machine accounts**, create one
   *per machine* (e.g. `ai-briefing-macbook`, `ai-briefing-desktop`) so each
   machine's access can be revoked independently. Grant it "Can read" on the
   `ai-briefing` project, then generate an access token on that machine
   account (shown once).
3. Save that token to `data/bws_access_token` on the machine (gitignored,
   `chmod 600` — never commit it, never put it in `.env`). To get the token
   onto the machine without retyping a long secret, store it as an item in
   your regular Bitwarden password vault first (that one already syncs via
   the app/browser extension) and paste it out from there once.
4. Comment out the corresponding keys in that machine's local `.env` (see
   the header comment `.env` grows once you do this) so Bitwarden's value
   actually takes effect — a key left active in `.env` still wins over
   Bitwarden, same precedence rule as always.

**Running with Bitwarden secrets:** nothing to run differently —
`.venv/bin/python run.py` works as-is. `config.py`'s `_load_bitwarden()`
checks for `data/bws_access_token`; if present, it runs
`bws secret list --project-id ... -o env` and merges the result into
`os.environ` before `.env` is read (so `.env`'s existing always-wins
precedence is unchanged — commenting a key out there just means "use
Bitwarden's value" now, instead of "use nothing"). If the token file is
missing, or the `bws` CLI itself isn't installed, this step silently no-ops
and `.env` behaves exactly as before — Bitwarden is additive, never a hard
dependency, and there's exactly one entry point either way.

To rotate a secret going forward: edit it in Bitwarden (web vault or
`bws secret edit`), not in `.env` — the next run picks it up with no
per-machine action needed.

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

`sender.py` tries the Gmail API over HTTPS first once it's configured (see
below), falling back to SMTP only if the API call fails or hasn't been set
up yet. Setup is one-time and needs a real browser login (this can't be
scripted around — Google requires an explicit consent click):

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

**Publishing status matters.** While the Cloud Console OAuth consent screen
sits in "Testing" (Console → APIs & Services → OAuth consent screen →
Audience tab), Google kills the refresh token after 7 days regardless of
use — silent, discovered only when the next send throws `invalid_grant`.
Publishing the app ("Publish app" button on that same Audience tab, User
type: External) removes that specific cap. There's no API/gcloud surface to
check this status (only `iap oauth-brands`, which is unrelated and was
deprecated/shut down in March 2026) — set `GMAIL_OAUTH_PUBLISHED=1` by hand
once you've confirmed "In production" in Console, so the dashboard's
`token_status()` indicator stops warning about a cap that no longer applies.

Note publishing isn't a complete guarantee either — a token has been
observed dying with `invalid_grant` well inside 7 days even while already
published; the exact cause wasn't confirmed (candidates: the
Testing→Production transition itself invalidating pre-existing grants, or
an account-level security event). If it happens again, re-run
`scripts/setup_gmail_oauth.py` and treat repeat fast deaths as a signal to
check the Google Account's security activity log, not just re-mint blindly.

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
