"""Shared configuration: .env loading, paths, subscriptions list."""

import json
import os
import re
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENV_PATH = os.path.join(REPO_ROOT, ".env")
DATA_DIR = os.path.join(REPO_ROOT, "data")
BWS_TOKEN_PATH = os.path.join(DATA_DIR, "bws_access_token")
# Not a secret — a Secrets Manager project ID, already committed in
# scripts/run_with_secrets.sh's history. One Bitwarden org per project is
# assumed; a second project would need this to become configurable.
BWS_PROJECT_ID = "06181b82-2489-4fdc-bd51-b4b20115e88a"
FEEDS_DB_PATH = os.path.join(DATA_DIR, "feeds.db")
MCP_LOCK_PATH = os.path.join(DATA_DIR, ".mcp.lock")
# Separate from feeds.db (vendored-owned) and from the future workflow.db —
# a narrow durable slice covering only panel-submitted research tasks. See
# src/briefing/research_store.py.
RESEARCH_TASKS_DB_PATH = os.path.join(DATA_DIR, "research_tasks.db")
SUBSCRIPTIONS_PATH = os.path.join(REPO_ROOT, "subscriptions.json")
STYLE_PATH = os.path.join(REPO_ROOT, "newsletter_style.md")
RESEARCH_REQUESTS_PATH = os.path.join(REPO_ROOT, "research_requests.md")
ARCHIVE_DIR = os.path.join(REPO_ROOT, "archives")
LOG_PATH = os.path.join(REPO_ROOT, "briefing.log")


def _apply_env_lines(text: str):
    """Parse KEY=VALUE lines (dotenv-style) and set them in os.environ.
    Shared by _load_env() (a real file) and _load_bitwarden() (bws's
    stdout) — both need the same comment-stripping and quote-unwrapping."""
    for line in text.splitlines():
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


def _load_bitwarden():
    """Populate os.environ from Bitwarden Secrets Manager, if this machine
    has done the one-time setup (data/bws_access_token present) — see
    "Secrets Manager (Bitwarden, optional)" in README.md. Applied before
    _load_env() so .env's existing always-wins precedence is unchanged:
    commenting a key out in .env still means "use Bitwarden's value",
    exactly like it used to mean "use nothing"."""
    if not os.path.exists(BWS_TOKEN_PATH):
        return
    bws_bin = shutil.which("bws")
    if not bws_bin:
        fallback = os.path.expanduser("~/.local/bin/bws")
        if not os.path.exists(fallback):
            print("Bitwarden token present but bws CLI not found — skipping", flush=True)
            return
        bws_bin = fallback
    with open(BWS_TOKEN_PATH, encoding="utf-8") as f:
        token = f.read().strip()
    try:
        result = subprocess.run(
            [bws_bin, "secret", "list", BWS_PROJECT_ID, "-o", "env"],
            env={**os.environ, "BWS_ACCESS_TOKEN": token},
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Bitwarden secret fetch failed ({e}) — continuing without it", flush=True)
        return
    _apply_env_lines(result.stdout)


def _load_env():
    """Load key=value pairs from .env next to the repo root (no external deps).

    Overrides ambient shell exports (including whatever _load_bitwarden()
    just set) for the specific keys this project reads, rather than
    setdefault()'ing around them — several of these names (MAXPLUS_API_KEY
    in particular) are also used by unrelated tooling (e.g. Claude Code's
    own shell config) and can be exported globally in an interactive
    terminal. Without this, commenting out a key in .env to intentionally
    disable a provider silently does nothing if a same-named var happens to
    be exported elsewhere — .env is the explicit, per-project source of
    truth and must win."""
    _load_bitwarden()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            _apply_env_lines(f.read())


def _bind():
    """(Re)bind the module-level config constants from os.environ. Split out
    of module top-level so reload() can re-run it after a .env edit — the
    control panel's /settings route rewrites .env in the running process,
    where 'a fresh run picks it up' doesn't apply."""
    global MAXPLUS_API_KEY, MAXPLUS_MODEL, GEMINI_API_KEY, GEMINI_MODEL
    global CLAUDE_CLI_ENABLED, CLAUDE_CLI_MODEL
    global BEDROCK_ENABLED, BEDROCK_MODEL, BEDROCK_REGION, BEDROCK_PROFILE
    global PROVIDER_ORDER
    global GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_OAUTH_PUBLISHED, RECIPIENT_EMAIL, RECIPIENT_EMAILS
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
    # Claude on AWS Bedrock. Region-prefixed Bedrock model ID; Sonnet 5
    # verified accessible on this account.
    BEDROCK_ENABLED = os.environ.get("BEDROCK_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-5")
    BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "ap-southeast-1"))
    # Named AWS profile (~/.aws/credentials) for Bedrock calls specifically.
    # Without this, boto3-style resolution picks up AWS_ACCESS_KEY_ID/
    # AWS_SECRET_ACCESS_KEY from the ambient environment first — on a
    # machine where those are set system-wide (e.g. for Claude Code's own
    # CLAUDE_CODE_USE_BEDROCK), that identity silently wins over whatever
    # ~/.aws/credentials profile this pipeline actually intends to use, the
    # same shadowing problem _load_env() already guards MAXPLUS_API_KEY
    # against. Empty by default — unset means "use default resolution",
    # unchanged from prior behavior.
    BEDROCK_PROFILE = os.environ.get("BEDROCK_PROFILE", "")
    # Comma-separated provider chain for the Generate phase, tried in order.
    # Known names: bedrock, maxplus, gemini, claude-cli. Unknown names are
    # skipped with a log line (so a typo can't kill the 5am run).
    PROVIDER_ORDER = [
        p.strip().lower()
        for p in os.environ.get("PROVIDER_ORDER", "bedrock,gemini,maxplus,claude-cli").split(",")
        if p.strip()
    ]
    GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
    GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
    # Google's Cloud Console "Audience" tab has no API/gcloud surface (confirmed
    # 2026-08-26 — gcloud's only OAuth-brand command, `iap oauth-brands`, is
    # unrelated and deprecated) — this can't be auto-detected, so a human sets
    # it once to match Console reality. False (Testing) is the safe default:
    # it just means gmail_api.token_status() keeps warning before the real
    # 7-day Testing-mode refresh-token death; True (published) drops that
    # countdown since it no longer applies.
    GMAIL_OAUTH_PUBLISHED = os.environ.get("GMAIL_OAUTH_PUBLISHED", "0").strip().lower() not in ("0", "false", "no")
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

# Source health: consecutive same-source check_feeds failures (excluding
# runs where every checked source failed — that's a network-wide outage,
# not a per-source problem, see collector._update_failure_streaks) before
# warning, then auto-disabling.
FAILURE_WARN_THRESHOLD = 3
FAILURE_DISABLE_THRESHOLD = 5


def load_subscriptions() -> list[dict]:
    """Read subscriptions.json. Returns [] if the file doesn't exist yet."""
    if not os.path.exists(SUBSCRIPTIONS_PATH):
        return []
    with open(SUBSCRIPTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_subscriptions(subs: list[dict]):
    with open(SUBSCRIPTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(subs, f, indent=2)
        f.write("\n")


def load_style() -> str:
    if not os.path.exists(STYLE_PATH):
        return ""
    with open(STYLE_PATH, encoding="utf-8") as f:
        return f.read()


def restrict_to_owner_only(path: str):
    """Best-effort lock a secrets file (.env, the Gmail OAuth token) down to
    the current user. os.chmod(0o600) is a no-op for access control on
    Windows/NTFS — it only toggles the read-only attribute, so a write mode
    like 0o600 actually clears read-only and leaves the file readable by
    every account with filesystem access (confirmed: os.stat().st_mode
    reports 0o666 right back, even after this runs). icacls with
    /inheritance:r strips the inherited ACEs (where Users/Everyone get
    their access) and /grant:r replaces them with only the current user."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    import getpass
    import subprocess

    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME") or getpass.getuser()
    account = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{account}:F"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except Exception as e:
        print(f"WARNING: could not restrict {path} to owner-only ({e})", flush=True)
