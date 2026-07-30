#!/usr/bin/env bash
# One-time (or repeat-safe) environment setup for ai-briefing.
# Builds a Python 3.11 venv because the vendored MCP server pins
# rapidocr-onnxruntime<3.13, and installs the noapi-google-search-mcp
# fork with our yt-dlp stdout fix (upstream PR #8, not yet merged).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

MCP_FORK_SHA="df43b4da27a1938df55a899e2bddd1c71d487f8b"
MCP_FORK_URL="git+https://github.com/Jarkius/noapi-google-search-mcp.git@${MCP_FORK_SHA}"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required (https://docs.astral.sh/uv/). Install it first." >&2
    exit 1
fi

echo "==> Creating Python 3.11 venv at .venv"
uv venv --python 3.11 .venv

echo "==> Installing project (+ dashboard extras) + vendored MCP server (pinned @ ${MCP_FORK_SHA:0:12})"
uv pip install --python .venv/bin/python -e ".[panel]"
uv pip install --python .venv/bin/python --reinstall-package noapi-google-search-mcp "${MCP_FORK_URL}"

echo "==> Installing Playwright Chromium (headless browser for MCP tools)"
.venv/bin/playwright install chromium

echo "==> Setup complete. Activate with: source .venv/bin/activate"
