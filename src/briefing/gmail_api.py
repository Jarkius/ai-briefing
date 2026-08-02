"""Gmail API (HTTPS, port 443) send + inbox-search. PRIMARY transport once
configured: the office network blocks the raw SMTP/IMAP protocol ports
(465/587/993, TLS handshakes reset — confirmed 2026-07-24, "office network
is 443-only", TODO.md) but 443 always works. sender.py tries this first
and falls back to SMTP/IMAP only if an API call fails; until the one-time
OAuth setup is done, SMTP/IMAP are the only path.

Requires a one-time OAuth2 setup that a human must complete (Google Cloud
Console + browser consent) — see scripts/setup_gmail_oauth.py and the
"Gmail API fallback" section in README.md. Until that setup is done,
`is_configured()` returns False and callers should skip straight to
reporting the SMTP/IMAP failure, not treat this as available-but-broken.
"""

import base64
import json
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_PATH = os.path.join(config.REPO_ROOT, "data", "gmail_oauth_token.json")
CLIENT_SECRET_PATH = os.path.join(config.REPO_ROOT, "data", "gmail_oauth_client_secret.json")


def is_configured() -> bool:
    """True once the one-time human OAuth setup (scripts/setup_gmail_oauth.py)
    has produced a stored token. Both files live under data/ (gitignored,
    per-machine) — never committed, since the token grants mailbox access."""
    return os.path.exists(TOKEN_PATH)


# While the OAuth consent screen stays in Google's "Testing" publishing
# status, the refresh token dies after 7 days regardless of use — the
# failure is silent (invalid_grant on the next send, discovered at 5am).
# There's no API to extend it; re-consenting in a browser resets the clock.
# We can't automate the click, but we CAN warn before it happens.
TESTING_MODE_TOKEN_LIFETIME_DAYS = 7
WARN_WITHIN_DAYS = 2


def token_age_days() -> float | None:
    """Days since the token file was last (re)written — a fresh consent or
    a refresh both update mtime. None if not configured."""
    if not os.path.exists(TOKEN_PATH):
        return None
    import time

    return (time.time() - os.path.getmtime(TOKEN_PATH)) / 86400


def token_status() -> dict:
    """Panel-facing summary: not_configured | ok | expiring_soon | expired.
    'expiring_soon'/'expired' assume Testing-mode's 7-day refresh-token
    limit — harmless over-warning once the app is published, since a
    published token has no such deadline and refreshes silently on use."""
    age = token_age_days()
    if age is None:
        return {"state": "not_configured", "age_days": None, "days_left": None}
    days_left = TESTING_MODE_TOKEN_LIFETIME_DAYS - age
    if days_left <= 0:
        state = "expired"
    elif days_left <= WARN_WITHIN_DAYS:
        state = "expiring_soon"
    else:
        state = "ok"
    return {"state": state, "age_days": round(age, 1), "days_left": round(days_left, 1)}


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"Gmail API not set up — run scripts/setup_gmail_oauth.py once "
            f"(needs a one-time browser login, cannot be automated). "
            f"Missing: {TOKEN_PATH}"
        )
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Atomic replace, not truncate+write: a cron send and a dashboard
        # send can both hit refresh near expiry, and an interleaved write
        # would corrupt the token file — killing the PRIMARY transport on
        # every later run until a human re-runs the OAuth setup. os.replace
        # is atomic on POSIX and Windows; worst case now is one refresh
        # harmlessly overwriting the other's equally-valid token.
        tmp_path = TOKEN_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        # gmail.modify scope = full mailbox read+send; default umask leaves
        # the token world-readable. Owner-only, like .env.
        config.restrict_to_owner_only(tmp_path)
        os.replace(tmp_path, TOKEN_PATH)
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_get_credentials())


def run_oauth_consent(timeout: int = 180) -> None:
    """Opens a local browser for the human consent click and saves the
    resulting token — the interactive step nothing can automate away.
    Shared by scripts/setup_gmail_oauth.py and the panel's Settings 'Re-
    authorize' button; requires CLIENT_SECRET_PATH to already exist (that
    part IS a one-time manual Cloud Console step, done once per project)."""
    if not os.path.exists(CLIENT_SECRET_PATH):
        raise RuntimeError(
            f"missing {CLIENT_SECRET_PATH} — the Cloud Console OAuth client "
            "setup (scripts/setup_gmail_oauth.py's steps 1-3) hasn't been done"
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    try:
        creds = flow.run_local_server(port=0, timeout_seconds=timeout)
    except Exception:
        creds = flow.run_console()

    tmp_path = TOKEN_PATH + ".tmp"
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    config.restrict_to_owner_only(tmp_path)
    os.replace(tmp_path, TOKEN_PATH)


def send_email_via_api(subject: str, html: str) -> None:
    """Send one HTML email via the Gmail API (HTTPS/443). Raises on failure —
    mirrors sender.send_email's contract so callers can use either
    interchangeably."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AI Briefing <{config.GMAIL_ADDRESS}>"
    msg["To"] = ", ".join(config.RECIPIENT_EMAILS)
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _service().users().messages().send(userId="me", body={"raw": raw}).execute()


def already_sent_today_via_api(subject_contains: str) -> bool:
    """Gmail-API equivalent of sender.already_sent_today — same semantics
    (subject substring match, today's messages), different transport."""
    from datetime import datetime

    # Gmail interprets after: in the ACCOUNT's timezone while this uses the
    # machine's local date — near midnight, with mismatched timezones, the
    # two can disagree (as can the IMAP fallback's SINCE window). Accepted:
    # the scheduled run is ~5am local, nowhere near either boundary.
    today = datetime.now().strftime("%Y/%m/%d")
    query = f'subject:"{subject_contains}" after:{today}'
    result = _service().users().messages().list(userId="me", q=query, maxResults=1).execute()
    return bool(result.get("messages"))
