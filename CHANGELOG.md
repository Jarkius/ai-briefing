# Changelog

Starts from this entry — earlier history lives in `git log`, not backfilled
here to avoid guessing at intent for commits made before this file existed.

## Unreleased

### Added
- Bitwarden Secrets Manager integration (optional, additive to `.env`) —
  `config.py._load_bitwarden()` fetches secrets from a Secrets Manager
  project at startup via `bws secret list -o env` if
  `data/bws_access_token` is present, merging them into `os.environ` before
  `.env` is read (so `.env`'s always-wins precedence is unchanged). No-ops
  silently if the token or the `bws` CLI itself is missing — one entry
  point (`python run.py`) either way, nothing to run differently per
  machine. See "Secrets Manager (Bitwarden, optional)" in `README.md`.
- `GMAIL_OAUTH_PUBLISHED` config flag (`src/briefing/config.py`,
  `src/briefing/gmail_api.py`) — set once the Cloud Console OAuth consent
  screen is confirmed "In production" (Audience tab); suppresses the
  dashboard's Gmail-token-expiry warning, which otherwise keeps assuming
  the 7-day Testing-mode refresh-token cap even after it no longer applies.
  No API/gcloud surface exists to auto-detect Console publishing status, so
  this is a human-set flag, not an auto-detected one.

### Fixed
- `gmail_api.token_status()` no longer shows a false "expiring in Nd"
  warning once `GMAIL_OAUTH_PUBLISHED=1` is set.
- README's Gmail API fallback description corrected — the API is tried
  first once configured, SMTP is the fallback, not the other way around
  (the doc had this backwards).
- Test suite no longer makes live network calls to Bitwarden on every
  `config` reload (`tests/conftest.py` neutralizes `BWS_TOKEN_PATH`
  globally) — an earlier version of this change quietly made the full
  suite ~4x slower before this fixture was added.

### Known issues
- A Gmail OAuth refresh token has been observed dying with `invalid_grant`
  well inside the 7-day Testing-mode window even after the app was already
  "In production" — root cause unconfirmed (candidates: the
  Testing→Production transition itself invalidating pre-existing grants, or
  an account-level security event on the sending Gmail account). See the
  2026-08-26 session retrospective in `ψ/memory/retrospectives/2026-08/`
  for the investigation so far.
