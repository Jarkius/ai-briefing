"""Shared configuration: .env loading, paths, subscriptions list."""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENV_PATH = os.path.join(REPO_ROOT, ".env")
DATA_DIR = os.path.join(REPO_ROOT, "data")
FEEDS_DB_PATH = os.path.join(DATA_DIR, "feeds.db")
MCP_LOCK_PATH = os.path.join(DATA_DIR, ".mcp.lock")
SUBSCRIPTIONS_PATH = os.path.join(REPO_ROOT, "subscriptions.json")
STYLE_PATH = os.path.join(REPO_ROOT, "newsletter_style.md")
RESEARCH_REQUESTS_PATH = os.path.join(REPO_ROOT, "research_requests.md")
ARCHIVE_DIR = os.path.join(REPO_ROOT, "archives")
LOG_PATH = os.path.join(REPO_ROOT, "briefing.log")


def _load_env():
    """Load key=value pairs from .env next to the repo root (no external deps)."""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


_load_env()

MAXPLUS_API_KEY = os.environ.get("MAXPLUS_API_KEY", "")
# "gemini-3.5-flash" (the original hardcoded default) is no longer served by
# the maxplus pool as of 2026-07-23 — confirmed via a live 400 response
# listing available models. Configurable so a future pool change doesn't
# require a code edit.
MAXPLUS_MODEL = os.environ.get("MAXPLUS_MODEL", "gpt-5.5")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

REQUIRED_ENV = {
    "MAXPLUS_API_KEY": MAXPLUS_API_KEY,
    "GMAIL_ADDRESS": GMAIL_ADDRESS,
    "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
}


def require_env():
    """Exit with a clear message if required config is missing. Call at CLI entrypoints only."""
    missing = [name for name, val in REQUIRED_ENV.items() if not val]
    if missing:
        sys.exit(f"ERROR — missing config: {', '.join(missing)}. Copy .env.example to .env and fill it in.")


KNOWN_SOURCE_TYPES = {
    "news", "reddit", "hackernews", "github", "arxiv", "youtube", "podcast", "twitter",
}


def load_subscriptions() -> list[dict]:
    """Read subscriptions.json. Returns [] if the file doesn't exist yet."""
    if not os.path.exists(SUBSCRIPTIONS_PATH):
        return []
    with open(SUBSCRIPTIONS_PATH) as f:
        return json.load(f)


def save_subscriptions(subs: list[dict]):
    with open(SUBSCRIPTIONS_PATH, "w") as f:
        json.dump(subs, f, indent=2)
        f.write("\n")


def load_style() -> str:
    if not os.path.exists(STYLE_PATH):
        return ""
    with open(STYLE_PATH) as f:
        return f.read()
