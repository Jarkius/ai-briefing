"""Shared configuration: .env loading, paths, subscriptions list."""

import json
import os
import re
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
    """Load key=value pairs from .env next to the repo root (no external deps).

    Overrides ambient shell exports for the specific keys this project
    reads, rather than setdefault()'ing around them — several of these
    names (MAXPLUS_API_KEY in particular) are also used by unrelated
    tooling (e.g. Claude Code's own shell config) and can be exported
    globally in an interactive terminal. Without this, commenting out a
    key in .env to intentionally disable a provider silently does nothing
    if a same-named var happens to be exported elsewhere — .env is the
    explicit, per-project source of truth and must win."""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    # Tolerate the two ways people actually write .env files:
                    # inline comments (VALUE   # note) and quoted values —
                    # both otherwise corrupt the value silently (e.g. a
                    # recipient address with a comment glued on).
                    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    os.environ[key.strip()] = value


def _bind():
    """(Re)bind the module-level config constants from os.environ. Split out
    of module top-level so reload() can re-run it after a .env edit — the
    control panel's /settings route rewrites .env in the running process,
    where 'a fresh run picks it up' doesn't apply."""
    global MAXPLUS_API_KEY, MAXPLUS_MODEL, GEMINI_API_KEY, GEMINI_MODEL
    global CLAUDE_CLI_ENABLED, CLAUDE_CLI_MODEL
    global GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL, RECIPIENT_EMAILS
    global REQUIRED_ENV

    MAXPLUS_API_KEY = os.environ.get("MAXPLUS_API_KEY", "")
    # "gemini-3.5-flash" (the original hardcoded default) is no longer served by
    # the maxplus pool as of 2026-07-23 — confirmed via a live 400 response
    # listing available models. Configurable so a future pool change doesn't
    # require a code edit.
    MAXPLUS_MODEL = os.environ.get("MAXPLUS_MODEL", "gpt-5.5")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    # Safety-net provider tier: shells out to the `claude` CLI (uses the existing
    # subscription, no separate API quota) when both maxplus and Gemini fail.
    # Enabled by default so a quota wall doesn't need a code change to route
    # around; "sonnet" not "opus" since this runs unattended and per-call cost
    # should stay low.
    CLAUDE_CLI_ENABLED = os.environ.get("CLAUDE_CLI_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    CLAUDE_CLI_MODEL = os.environ.get("CLAUDE_CLI_MODEL", "sonnet")
    GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
    GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
    RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)
    # Comma-separated in .env; every send path needs the parsed list (SMTP's
    # to_addrs and Outlook's To take all recipients, a bare comma string would
    # silently deliver to only the first).
    RECIPIENT_EMAILS = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]

    REQUIRED_ENV = {
        "GMAIL_ADDRESS": GMAIL_ADDRESS,
        "GMAIL_APP_PASSWORD": GMAIL_APP_PASSWORD,
    }


_load_env()
_bind()


def reload():
    """Re-read .env and rebind all module constants. For long-lived processes
    (the control panel) after a settings edit; CLI runs never need it.

    Note: callers using `from briefing.config import X` hold stale snapshots —
    always access via `config.X` (the existing codebase already does)."""
    _load_env()
    _bind()


def require_env():
    """Exit with a clear message if required config is missing. Call at CLI entrypoints only."""
    missing = [name for name, val in REQUIRED_ENV.items() if not val]
    # The app password only feeds the SMTP/IMAP fallback — once the Gmail
    # API OAuth token exists (the primary transport), a missing password
    # must not block the run. Local import: gmail_api imports config, so a
    # top-level import here would be circular.
    if "GMAIL_APP_PASSWORD" in missing:
        from . import gmail_api

        if gmail_api.is_configured():
            missing.remove("GMAIL_APP_PASSWORD")
    # The generator's provider chain accepts any of three backends —
    # requiring an API key would block a Claude-CLI-only setup (the CLI is
    # a full provider tier, not just a fallback). shutil.which mirrors the
    # generator's own availability check.
    if not (MAXPLUS_API_KEY or GEMINI_API_KEY):
        import shutil

        if not (CLAUDE_CLI_ENABLED and shutil.which("claude")):
            missing.append("MAXPLUS_API_KEY or GEMINI_API_KEY (or claude CLI on PATH)")
    # A blank/comma-only RECIPIENT_EMAIL parses to [] and would only surface
    # at send time, after a full collect/generate cycle.
    if not RECIPIENT_EMAILS:
        missing.append("RECIPIENT_EMAIL")
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
