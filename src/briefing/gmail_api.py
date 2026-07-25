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
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_get_credentials())


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
