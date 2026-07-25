#!/usr/bin/env bash
# Launch the control panel — localhost ONLY (credentials are editable via
# /settings; never change this bind address without adding auth first).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if ! .venv/bin/python -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "ERROR: dashboard deps missing — run ./setup.sh (installs .[panel])." >&2
    exit 1
fi

( sleep 1.5; .venv/bin/python -m webbrowser "http://127.0.0.1:8787/preview" ) &
exec .venv/bin/python -m uvicorn src.panel.app:app --host 127.0.0.1 --port 8787 "$@"
