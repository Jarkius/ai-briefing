#!/usr/bin/env python3
"""
Daily AI Briefing v2 - Web Search Fallback Edition
Uses Claude's web_search when direct network access is restricted
"""

import json, sys, smtplib, os, subprocess
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_env():
    """Load key=value pairs from .env next to this script (no external deps)."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

_load_env()

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)
ARCHIVE_DIR        = os.path.join(SCRIPT_DIR, "archives")

if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
    sys.exit("ERROR — missing config: GMAIL_ADDRESS / GMAIL_APP_PASSWORD. Copy .env.example to .env and fill it in.")

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_email(subject: str, html_body: str) -> bool:
    """Send HTML email via Gmail SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = RECIPIENT_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log(f"  Email failed: {e}")
        return False

def markdown_to_html(md_content: str) -> str:
    """Convert markdown briefing to HTML email format."""
    html = """
    <html>
    <head>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                   line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
            h1 { color: #1a1a1a; border-bottom: 3px solid #4A90E2; padding-bottom: 10px; }
            h2 { color: #2c3e50; margin-top: 30px; border-left: 4px solid #4A90E2; padding-left: 15px; }
            h3 { color: #34495e; margin-top: 20px; }
            a { color: #4A90E2; text-decoration: none; }
            a:hover { text-decoration: underline; }
            code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }
            ul, ol { padding-left: 25px; }
            li { margin: 8px 0; }
            strong { color: #2c3e50; }
            hr { border: none; border-top: 2px solid #eee; margin: 30px 0; }
            .timestamp { color: #7f8c8d; font-size: 0.9em; font-style: italic; }
        </style>
    </head>
    <body>
    """

    # Simple markdown to HTML conversion
    lines = md_content.split('\n')
    for line in lines:
        if line.startswith('# '):
            html += f"<h1>{line[2:]}</h1>\n"
        elif line.startswith('## '):
            html += f"<h2>{line[3:]}</h2>\n"
        elif line.startswith('### '):
            html += f"<h3>{line[4:]}</h3>\n"
        elif line.startswith('**Date:**'):
            html += f"<p class='timestamp'>{line}</p>\n"
        elif line.startswith('---'):
            html += "<hr>\n"
        elif line.startswith('- '):
            html += f"<li>{line[2:]}</li>\n"
        elif line.strip() == '':
            html += "<br>\n"
        else:
            html += f"<p>{line}</p>\n"

    html += "</body></html>"
    return html

def main():
    log("Starting AI Briefing v2 (Web Search Mode)")

    # Check if today's briefing already exists
    today = datetime.now().strftime('%Y-%m-%d')
    archive_files = [f for f in os.listdir(ARCHIVE_DIR) if f.startswith(f"briefing_{today}")]

    if not archive_files:
        log("ERROR: No briefing file found for today. Run web search compilation first.")
        sys.exit(1)

    # Get the most recent briefing
    latest_briefing = sorted(archive_files)[-1]
    briefing_path = os.path.join(ARCHIVE_DIR, latest_briefing)

    log(f"Found briefing: {latest_briefing}")

    with open(briefing_path, 'r') as f:
        md_content = f.read()

    # Split content into two parts
    sections = md_content.split('\n## ')

    # Part 1: Top stories through Prompt Engineering (sections 1-6)
    part1_sections = [sections[0]] + [f"## {s}" for s in sections[1:7]]
    part1_md = '\n'.join(part1_sections)

    # Part 2: Security through Community (sections 7-11)
    part2_sections = [f"## {s}" for s in sections[7:]]
    part2_md = '# AI Awareness Briefing - Part 2\n' + '\n'.join(part2_sections)

    # Convert to HTML
    part1_html = markdown_to_html(part1_md)
    part2_html = markdown_to_html(part2_md)

    # Send emails
    log("Sending Part 1: News & Learning...")
    if send_email(f"🤖 AI Briefing Part 1: News & Learning - {today}", part1_html):
        log("  ✓ Part 1 sent")

    log("Sending Part 2: Technical & Community...")
    if send_email(f"🤖 AI Briefing Part 2: Technical & Community - {today}", part2_html):
        log("  ✓ Part 2 sent")

    log("✓ Briefing delivery complete")

if __name__ == "__main__":
    main()
