#!/usr/bin/env python3
"""One-time setup for the Gmail API fallback (src/briefing/gmail_api.py).

This CANNOT be fully automated: Google's OAuth consent screen requires you
to click "Allow" in a real browser, logged in as the Gmail account this
project sends from. Run this once, interactively, then never again (the
resulting token auto-refreshes).

Steps this script walks you through:
  1. Create a Google Cloud project + enable the Gmail API (one-time, in
     your browser — this script prints the exact URLs).
  2. Create an OAuth "Desktop app" client, download its client secret JSON,
     save it to data/gmail_oauth_client_secret.json.
  3. Run this script — it opens a browser for you to click Allow, then
     saves the resulting token to data/gmail_oauth_token.json.

After this, src/briefing/gmail_api.is_configured() returns True and the
pipeline can send/check mail over HTTPS (port 443) even on networks that
block raw SMTP/IMAP.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from briefing import config, gmail_api  # noqa: E402

INSTRUCTIONS = f"""
Gmail API one-time setup
=========================

This needs a few manual clicks in your browser — there's no way around
Google's consent requirement, but it's a five-minute, one-time task.

1. Go to https://console.cloud.google.com/apis/library/gmail.googleapis.com
   (log in as {config.GMAIL_ADDRESS or 'your Gmail account'})
   - Create a project if you don't have one (top-left project picker → New Project)
   - Click "Enable" on the Gmail API page

2. Go to https://console.cloud.google.com/apis/credentials
   - Click "+ Create Credentials" → "OAuth client ID"
   - If prompted, configure the OAuth consent screen first:
     - User type: External, App name: "AI Briefing" (or anything), your email
       as support/developer contact — you can leave scopes/test users default
     - Add yourself ({config.GMAIL_ADDRESS or 'your email'}) as a test user
   - Application type: "Desktop app", name: "AI Briefing CLI"
   - Click "Create", then "Download JSON" on the resulting client ID

3. Save that downloaded file to:
   {gmail_api.CLIENT_SECRET_PATH}

4. Press Enter here to continue once step 3 is done.
"""


def main():
    os.makedirs(os.path.dirname(gmail_api.CLIENT_SECRET_PATH), exist_ok=True)

    if not os.path.exists(gmail_api.CLIENT_SECRET_PATH):
        print(INSTRUCTIONS)
        input()
        if not os.path.exists(gmail_api.CLIENT_SECRET_PATH):
            sys.exit(
                f"Still missing {gmail_api.CLIENT_SECRET_PATH} — "
                "download the client secret JSON from step 2 and save it there, then re-run."
            )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        gmail_api.CLIENT_SECRET_PATH, gmail_api.SCOPES,
    )

    print("\nOpening a browser for Google's consent screen...")
    print("(If no browser opens — e.g. you're on a headless/remote machine —")
    print(" this will print a URL to open manually and a code to paste back.)\n")
    try:
        creds = flow.run_local_server(port=0)
    except Exception:
        creds = flow.run_console()

    with open(gmail_api.TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print(f"\nDone. Token saved to {gmail_api.TOKEN_PATH}.")
    print("The Gmail API fallback is now active — run.py will use it")
    print("automatically if SMTP/IMAP fail.")


if __name__ == "__main__":
    main()
