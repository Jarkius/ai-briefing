#!/bin/bash
# Runs run.py with secrets injected from Bitwarden Secrets Manager instead of
# a local .env file — the access token at data/bws_access_token is the only
# per-machine credential left; everything else (GMAIL_ADDRESS, API keys, etc.)
# lives in the "ai-briefing" Secrets Manager project and updates there reach
# every machine on the next run, with no manual .env copy/paste.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="06181b82-2489-4fdc-bd51-b4b20115e88a"
BWS_BIN="$HOME/.local/bin/bws"
TOKEN_PATH="$REPO_ROOT/data/bws_access_token"

# Bitwarden is additive, not required — a machine with no token file yet
# (fresh clone, or one that hasn't done the one-time Bitwarden setup) just
# runs run.py directly against its own local .env, same as always.
if [ ! -f "$TOKEN_PATH" ]; then
  exec "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/run.py" "$@"
fi

export BWS_ACCESS_TOKEN="$(cat "$TOKEN_PATH")"

exec "$BWS_BIN" run --project-id "$PROJECT_ID" -- \
  "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/run.py" "$@"
