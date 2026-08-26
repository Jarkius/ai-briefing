# Changelog

Starts from this entry — earlier history lives in `git log`, not backfilled
here to avoid guessing at intent for commits made before this file existed.

## Unreleased

### Added
- Bitwarden Secrets Manager integration (optional, additive to `.env`) —
  `scripts/run_with_secrets.sh` injects secrets from a Secrets Manager
  project at runtime via `bws run`, so the same secrets sync across
  machines without manually copying `.env`. Falls back to running `run.py`
  directly (against local `.env`) if no access token is present. See
  "Secrets Manager (Bitwarden, optional)" in `README.md`.
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
- `com.user.ai-briefing.plist` (both the tracked template and the installed
  copy) now points at `scripts/run_with_secrets.sh` instead of calling
  `run.py` directly, and README's Gmail API fallback description corrected
  — the API is tried first once configured, SMTP is the fallback, not the
  other way around (the doc had this backwards).

### Known issues
- A Gmail OAuth refresh token has been observed dying with `invalid_grant`
  well inside the 7-day Testing-mode window even after the app was already
  "In production" — root cause unconfirmed (candidates: the
  Testing→Production transition itself invalidating pre-existing grants, or
  an account-level security event on the sending Gmail account). See the
  2026-08-26 session retrospective in `ψ/memory/retrospectives/2026-08/`
  for the investigation so far.
