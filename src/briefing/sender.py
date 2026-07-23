"""Markdown -> HTML rendering, two-part email split, SMTP send, and the
IMAP pre-check that makes send cross-machine-idempotent.

The markdown->HTML function here is deliberately a plain, side-effect-free,
string-returning function (per .omc/plans/2026-07-22-control-panel.md step 7)
so the dashboard's /preview route can call it directly and get byte-identical
output to what actually gets emailed — no second render path.
"""

import imaplib
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config


def _inline(text: str) -> str:
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  r'<a href="\2" style="color:#3b82f6">\1</a>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    return text


def markdown_to_html(text: str, date_str: str, title: str = "Daily AI Briefing") -> str:
    """Convert markdown to newsletter-ready HTML with section cards, callouts,
    and social-post styling. Pure function: same input always produces the
    same output, no file/network access."""
    lines, parts = text.split("\n"), []
    i = 0

    while i < len(lines):
        s = lines[i].strip()

        if s.startswith("## "):
            parts.append(
                f'<div style="margin:32px 0 20px;padding:16px;background:#f8fafc;border-left:4px solid #3b82f6;border-radius:8px">'
                f'<h2 style="color:#1e293b;margin:0;font-size:20px;font-weight:600">{_inline(s[3:])}</h2>'
                f'</div>'
            )
        elif s.startswith("**") and "**" in s[2:]:
            end_idx = s.index("**", 2)
            headline = s[2:end_idx]
            rest = s[end_idx + 2:].strip()

            card = f'<div style="margin:24px 0;padding:20px;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08)">'
            card += f'<h3 style="margin:0 0 12px;color:#0f172a;font-size:18px;font-weight:600;line-height:1.4">{_inline(headline)}</h3>'

            body_lines = [rest] if rest else []
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if next_line.startswith(("**", "## ", "📱", "---", "")) or next_line.startswith("#"):
                    break
                body_lines.append(next_line)
                i += 1
            i -= 1

            for body_line in body_lines:
                if body_line.startswith("**Key"):
                    card += f'<div style="margin:12px 0 8px;padding:10px;background:#fef3c7;border-left:3px solid #f59e0b;border-radius:4px"><strong style="color:#92400e">💡 {_inline(body_line[2:])}</strong></div>'
                elif body_line.startswith("**Why"):
                    card += f'<div style="margin:12px 0 8px;padding:10px;background:#dbeafe;border-left:3px solid #3b82f6;border-radius:4px"><strong style="color:#1e40af">🎯 {_inline(body_line[2:])}</strong></div>'
                elif body_line.startswith("📱"):
                    social = body_line.replace("📱 Social post:", "").replace("📱", "").strip()
                    card += f'<div style="margin:16px 0 8px;padding:12px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px">'
                    card += f'<div style="color:#15803d;font-size:13px;font-weight:600;margin-bottom:6px">📱 READY TO SHARE</div>'
                    card += f'<div style="color:#166534;font-size:14px;line-height:1.5">{_inline(social)}</div>'
                    card += '</div>'
                elif body_line.startswith("[Source]") or body_line.startswith("Source:"):
                    card += f'<div style="margin:12px 0 0;padding-top:12px;border-top:1px solid #e2e8f0"><span style="font-size:12px;color:#64748b">🔗 {_inline(body_line)}</span></div>'
                elif "#" in body_line and body_line.startswith("#"):
                    card += f'<div style="margin:8px 0 0"><span style="font-size:13px;color:#3b82f6">{_inline(body_line)}</span></div>'
                else:
                    card += f'<p style="margin:8px 0;color:#334155;line-height:1.7">{_inline(body_line)}</p>'

            card += '</div>'
            parts.append(card)
        elif s == "---":
            parts.append('<hr style="border:none;border-top:2px solid #e2e8f0;margin:32px 0">')
        elif s and not s.startswith(("##", "**", "📱", "#")):
            parts.append(f'<p style="margin:12px 0;color:#475569;line-height:1.7">{_inline(s)}</p>')

        i += 1

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
             max-width:680px;margin:0 auto;padding:20px;background:#f8fafc">

  <div style="background:linear-gradient(135deg,#0f172a,#1e293b);padding:28px 32px;border-radius:16px;margin-bottom:32px;box-shadow:0 4px 6px rgba(0,0,0,0.1)">
    <h1 style="color:#fff;margin:0;font-size:26px;font-weight:700;letter-spacing:-0.5px">🤖 {title}</h1>
    <p style="color:#cbd5e1;margin:8px 0 0;font-size:14px">{date_str}</p>
  </div>

  <div style="background:#ffffff;padding:32px;border-radius:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
    {body}
  </div>

  <div style="margin-top:32px;padding:20px;text-align:center;color:#94a3af;font-size:13px;background:#ffffff;border-radius:12px">
    <p style="margin:0 0 8px"><strong>Sources:</strong> Feeds · YouTube · Web Search · Gemini AI</p>
    <p style="margin:0">Curated by AI · Delivered with ❤️</p>
  </div>

</body></html>"""


def split_two_parts(markdown_text: str) -> tuple[str, str]:
    """Split a full briefing markdown doc into (part1_md, part2_md) at the
    7th top-level section, matching the existing newsletter structure
    (News & Learning / Technical & Community)."""
    sections = markdown_text.split("\n## ")
    part1 = "\n".join([sections[0]] + [f"## {s}" for s in sections[1:7]])
    part2 = "# AI Briefing — Part 2\n" + "\n".join(f"## {s}" for s in sections[7:])
    return part1, part2


def send_email(subject: str, html: str) -> None:
    """Send one HTML email via Gmail SMTP. Raises on failure — callers decide
    whether that's fatal (CLI) or a banner (dashboard)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AI Briefing <{config.GMAIL_ADDRESS}>"
    msg["To"] = config.RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.sendmail(config.GMAIL_ADDRESS, config.RECIPIENT_EMAIL, msg.as_string())


def already_sent_today(subject_contains: str) -> bool:
    """IMAP pre-check: does today's mailbox already contain a message whose
    subject contains this string? This is the cross-machine dedup source of
    truth (SMTP itself has no idempotency) — see mcp-integration plan
    architecture decision 4."""
    today_imap = datetime.now().strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, f'(SINCE "{today_imap}" SUBJECT "{subject_contains}")')
        ids = data[0].split() if data and data[0] else []
        return len(ids) > 0


def send_two_part_briefing(part1_html: str, part2_html: str, date_str: str) -> dict:
    """Send both parts, skipping whichever already exists in the mailbox
    today. Returns a status dict per part: 'sent' | 'already_sent' | 'error'."""
    result = {}
    subjects = {
        "part1": f"🤖 AI Briefing Part 1: News & Learning — {date_str}",
        "part2": f"💻 AI Briefing Part 2: Technical & Community — {date_str}",
    }
    htmls = {"part1": part1_html, "part2": part2_html}

    for part, subject in subjects.items():
        marker = subject.split(" — ")[0]  # e.g. "🤖 AI Briefing Part 1: News & Learning"
        try:
            if already_sent_today(marker):
                result[part] = "already_sent"
                continue
            send_email(subject, htmls[part])
            result[part] = "sent"
        except Exception as e:
            result[part] = f"error: {e}"
    return result
