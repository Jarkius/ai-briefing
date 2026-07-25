"""Markdown -> HTML rendering, two-part email split, SMTP send, and the
IMAP pre-check that makes send cross-machine-idempotent.

The markdown->HTML function here is deliberately a plain, side-effect-free,
string-returning function (per .omc/plans/2026-07-22-control-panel.md step 7)
so the dashboard's /preview route can call it directly and get byte-identical
output to what actually gets emailed — no second render path.
"""

import html as html_lib
import imaplib
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config


def _with_retry(fn, max_attempts: int = 3, label: str = "operation"):
    """Retry a zero-arg callable with backoff. Gmail's TLS handshake on
    this network has been observed to reset intermittently (confirmed via
    raw-socket tests independent of any library) — a single failure should
    not fail the whole send/check, since the very next attempt often
    succeeds."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
    raise last_error


def _inline(text: str) -> str:
    # Escape first: feed titles/URLs are attacker-controlled and land in a
    # trusted daily email — raw HTML or attribute-breaking quotes must never
    # pass through. Only http(s) links are rendered as anchors.
    text = html_lib.escape(text, quote=True)

    def _link(m):
        label, url = m.group(1), m.group(2)
        if not url.startswith(("http://", "https://")):
            return label
        return f'<a href="{url}" style="color:#26890D">{label}</a>'

    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', _link, text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    return text


# Section → (tag label, tag colour, accent colour) — AI Pulse newsletter
# styling ported from the Windows branch's ai_briefing.py (now legacy/).
_SECTION_STYLES = {
    "top 3": ("🔥 Top Stories", "#D04A02", "#D04A02"),
    "news": ("📰 AI News", "#00A3E0", "#00A3E0"),
    "governance": ("🏛️ Governance & Policy", "#6B21A8", "#6B21A8"),
    "mindset": ("🧠 Mindset & Culture", "#0369A1", "#0369A1"),
    "learning": ("📚 Learning", "#26890D", "#26890D"),
    "prompt": ("🎯 Prompt Engineering", "#0D9488", "#0D9488"),
    "security": ("🔒 Security & Privacy", "#B91C1C", "#B91C1C"),
    "ethics": ("⚖️ Ethics", "#92400E", "#B45309"),
    "research": ("🔬 Research", "#1D4ED8", "#1D4ED8"),
    "tools": ("💻 Tools & Resources", "#26890D", "#26890D"),
    "community": ("💬 Community", "#5B21B6", "#5B21B6"),
}

_DEFAULT_SECTION = ("📋 Briefing", "#26890D", "#26890D")


def _section_style(header_text: str):
    h = header_text.lower()
    for key, val in _SECTION_STYLES.items():
        if key in h:
            return val
    return _DEFAULT_SECTION

_CALLOUT_RE = re.compile(
    r'\*\*(Key takeaway|Why it matters|Action to take|What to consider|Key insight|Key feature)[:\*]',
    re.I,
)


def markdown_to_html(text: str, date_str: str, title: str = "Daily AI Briefing") -> str:
    """Convert briefing markdown to AI Pulse-style newsletter HTML — the
    Deloitte-branded table layout from the Windows branch's ai_briefing.py
    (legacy/), kept table-based for Outlook compatibility. Pure function:
    same input always produces the same output, no file/network access."""
    lines = text.split("\n")
    articles = []
    i = 0
    current_section = _DEFAULT_SECTION

    while i < len(lines):
        s = lines[i].strip()

        # Section header → coloured tag + underlined heading
        if s.startswith("## "):
            header_text = s[3:]
            current_section = _section_style(header_text)
            tag_label, tag_color, accent = current_section
            articles.append(f"""
  <tr><td style="padding:20px 28px 0 28px;">
    <p style="margin:0 0 4pt 0;font-size:8pt;font-weight:bold;letter-spacing:1pt;
              text-transform:uppercase;display:inline-block;padding:3px 10px;
              border-radius:2px;color:white;background:{tag_color};">&#9632; {tag_label}</p>
    <h2 style="margin:6pt 0 0 0;font-size:13pt;font-weight:bold;color:{accent};
               text-transform:uppercase;letter-spacing:.3pt;border-bottom:2px solid {accent};
               padding-bottom:6pt;">{_inline(header_text)}</h2>
  </td></tr>""")

        # Story card (bold headline)
        elif s.startswith("**") and "**" in s[2:]:
            end_idx = s.index("**", 2)
            headline = s[2:end_idx]
            rest = s[end_idx + 2:].strip()
            _, _, accent = current_section

            body_lines = [rest] if rest else []
            i += 1
            while i < len(lines):
                nl = lines[i].strip()
                # Card ends at the next section/divider/headline; **Key…/**Why…
                # callouts stay in this card (handled below). A blank line only
                # ends the card when the next line starts a new headline.
                if nl.startswith(("## ", "---")):
                    break
                if nl.startswith("**") and not _CALLOUT_RE.match(nl):
                    break
                if nl == "" and i + 1 < len(lines) and lines[i + 1].strip().startswith("**") \
                        and not _CALLOUT_RE.match(lines[i + 1].strip()):
                    break
                body_lines.append(nl)
                i += 1
            i -= 1

            body_html = ""
            for bl in body_lines:
                if not bl:
                    continue
                # Key takeaway / Why it matters → accent-border callout
                if _CALLOUT_RE.match(bl):
                    label = re.sub(r'\*\*([^*]+)\*\*.*', r'\1', bl).rstrip(':')
                    rest_bl = re.sub(r'\*\*[^*]+\*\*:?\s*', '', bl).strip()
                    body_html += f"""
        <table border=0 cellspacing=0 cellpadding=0 width="100%" style="border-collapse:collapse;margin:10pt 0 0 0;">
          <tr><td style="background:#f5f9f0;padding:10px 14px;border-left:3px solid {accent};">
            <p style="margin:0;font-size:10pt;color:{accent};font-weight:bold;">{_inline(label)}:</p>
            <p style="margin:4pt 0 0 0;font-size:10pt;color:#1a1a1a;">{_inline(rest_bl)}</p>
          </td></tr>
        </table>"""
                # Social post → subtle grey box
                elif bl.startswith("📱"):
                    social = re.sub(r'^📱\s*(Social post:?)?\s*', '', bl).strip()
                    body_html += f"""
        <table border=0 cellspacing=0 cellpadding=0 width="100%" style="border-collapse:collapse;margin:10pt 0 0 0;">
          <tr><td style="background:#f8f8f8;padding:8px 12px;border-left:3px solid #ccc;">
            <p style="margin:0;font-size:8pt;font-weight:bold;color:#888;letter-spacing:.5pt;">📱 SHARE-READY POST</p>
            <p style="margin:4pt 0 0 0;font-size:10pt;color:#444;">{_inline(social)}</p>
          </td></tr>
        </table>"""
                # Source link
                elif bl.startswith(("[Source", "Source:")):
                    body_html += f'<p style="margin:8pt 0 0 0;font-size:9pt;color:#888;">🔗 {_inline(bl)}</p>'
                else:
                    body_html += f'<p style="margin:0 0 6pt 0;font-size:11pt;color:#1a1a1a;line-height:1.6;">{_inline(bl)}</p>'

            articles.append(f"""
  <tr><td style="padding:16px 28px 8px 28px;">
    <h3 style="margin:0 0 8pt 0;font-size:12pt;font-weight:bold;color:#0f172a;line-height:1.4;">{_inline(headline)}</h3>
    {body_html}
  </td></tr>
  <tr><td style="padding:4px 28px;"><div style="border-top:1px solid #eee;"></div></td></tr>""")

        # Section divider (---) → green rule
        elif s == "---":
            articles.append("""
  <tr><td style="padding:12px 28px;">
    <div style="border-top:2px solid #86BC25;"></div>
  </td></tr>""")

        # Regular paragraph (intro note, etc.)
        elif s and not s.startswith(("##", "**", "📱", "#")):
            articles.append(f"""
  <tr><td style="padding:4px 28px;">
    <p style="margin:0 0 6pt 0;font-size:11pt;color:#555;font-style:italic;line-height:1.6;">{_inline(s)}</p>
  </td></tr>""")

        i += 1

    body_rows = "\n".join(articles)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<style>
body {{margin:0;padding:0;background:#F2F2F2;font-family:"Aptos",Calibri,sans-serif;}}
p {{margin:0 0 8pt 0;font-size:11pt;color:#1a1a1a;line-height:1.6;}}
h2 {{margin:14pt 0 6pt 0;font-size:12pt;font-weight:bold;color:#26890D;text-transform:uppercase;letter-spacing:.3pt;}}
a {{color:#26890D;}}
</style>
</head>
<body bgcolor="#F2F2F2">
<table border=0 cellspacing=0 cellpadding=0 width=600 align=center style="background:#F2F2F2;border-collapse:collapse;">

  <!-- BREADCRUMB -->
  <tr>
    <td style="padding:6px 20px 4px 20px;background:#F2F2F2;">
      <p style="margin:0;font-size:7pt;color:#7F7F7F;line-height:1.5;">Southeast Asia &nbsp;|&nbsp; Information Technology &nbsp;|&nbsp; {date_str}</p>
    </td>
  </tr>

  <!-- MASTHEAD -->
  <tr>
    <td style="padding:0 5px 5px 5px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="background:#0f172a;border-collapse:collapse;">
        <tr>
          <td style="padding:22px 28px 24px 28px;">
            <p style="margin:0 0 4pt 0;font-size:20pt;font-weight:bold;color:white;letter-spacing:-0.5pt;">🤖 {title}</p>
            <p style="margin:0 0 6pt 0;font-size:11pt;color:#94a3b8;font-style:italic;">What happened in AI today — curated for SEA IT</p>
            <p style="margin:0;font-size:8pt;color:#475569;border-top:1px solid #334155;padding-top:8pt;">{date_str} &nbsp;&#8226;&nbsp; Sources: Feeds · YouTube · Web Search</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- BODY -->
  <tr>
    <td style="padding:0 5px 5px 5px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="background:white;border-collapse:collapse;">
        <tr><td style="border-top:3px solid #86BC25;padding:0;"></td></tr>
        {body_rows}
        <tr><td style="padding:16px 28px 24px 28px;">
          <p style="margin:0;font-size:9pt;color:#94a3b8;">This briefing is AI-generated from public sources. Verify before acting on any item.</p>
        </td></tr>
      </table>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="padding:16px 20px 20px 20px;">
      <table border=0 cellspacing=0 cellpadding=0 width=590 style="border-collapse:collapse;">
        <tr>
          <td style="padding:16px 20px;background:#1a1a1a;text-align:center;">
            <p style="margin:0 0 4pt 0;font-size:9pt;color:#86BC25;font-weight:bold;letter-spacing:.5pt;text-transform:uppercase;">SEA IT · AI Hub</p>
            <p style="margin:0 0 4pt 0;font-size:8pt;color:#aaa;">This communication is intended solely for Deloitte SEA IT personnel.</p>
            <p style="margin:0;font-size:8pt;color:#666;">Confidential — For Internal Use Only &nbsp;&#8226;&nbsp; &copy; Deloitte {date_str[-4:]}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>

</table>
</body>
</html>"""


def split_two_parts(markdown_text: str) -> tuple[str, str]:
    """Split a full briefing markdown doc into (part1_md, part2_md) at the
    7th top-level section, matching the existing newsletter structure
    (News & Learning / Technical & Community)."""
    sections = markdown_text.split("\n## ")
    part1 = "\n".join([sections[0]] + [f"## {s}" for s in sections[1:7]])
    part2 = "# AI Briefing — Part 2\n" + "\n".join(f"## {s}" for s in sections[7:])
    return part1, part2


def _send_via_outlook(subject: str, html: str) -> None:
    """Send using the local Outlook client via PowerShell COM automation —
    port of legacy/ai_briefing.py:_send_via_outlook (lines 811-834). Only
    meaningful on win32 (requires a local Outlook install + interactive
    desktop session; COM automation is unavailable headless/non-Windows).

    The html body and subject are NEVER inlined into the PowerShell command
    string — subject/body content originates from LLM output over
    untrusted feed data, and interpolating it into a shell command would be
    a command-injection vector. Instead: the html is written to a temp file
    that the PS script reads with [IO.File]::ReadAllText, and the subject is
    escaped for a PS single-quoted string ('' doubling) and passed as its
    own literal."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            tmp_path = f.name

        escaped_subject = subject.replace("'", "''")
        ps_script = f"""
$ol = New-Object -ComObject Outlook.Application
$mail = $ol.CreateItem(0)
$mail.To = '{config.RECIPIENT_EMAIL}'
$mail.Subject = '{escaped_subject}'
$mail.HTMLBody = [IO.File]::ReadAllText('{tmp_path}')
$mail.Send()
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:200])
    finally:
        if tmp_path is not None:
            os.remove(tmp_path)


def send_email(subject: str, html: str) -> None:
    """Send one HTML email via Gmail SMTP. Raises on failure — callers decide
    whether that's fatal (CLI) or a banner (dashboard).

    Tries SMTP_SSL:465 first, falls back to STARTTLS:587. Observed on this
    network: TLS handshakes to smtp.gmail.com are intermittently reset —
    465 succeeded when 587 failed in back-to-back tests, and this pattern
    reproduced with raw sockets (no smtplib involved), so it's network-level
    flakiness, not an smtplib/library bug. Trying both gives each send the
    best chance of getting through a transient block on one path.

    On win32, if both SMTP attempts fail, falls back to a local Outlook COM
    send (see _send_via_outlook) — the office network this laptop runs on
    is 443-only and blocks SMTP entirely, so Outlook's own connection is the
    only path out.

    On any platform, if SMTP fails AND the Gmail API is configured (see
    gmail_api.py / scripts/setup_gmail_oauth.py), falls back to sending
    over HTTPS via the Gmail API — the same 443-only-network problem can
    hit macOS too (confirmed independently by raw-socket testing), and
    macOS has no Outlook COM to fall back to."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AI Briefing <{config.GMAIL_ADDRESS}>"
    msg["To"] = config.RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    # `with smtplib.SMTP(...) as server:` calls server.quit() on exit, which
    # sends its own QUIT command over the (possibly already-flaky)
    # connection. smtplib's __exit__ only swallows SMTPServerDisconnected —
    # a ConnectionResetError/OSError during that post-send QUIT propagates
    # as if the whole call failed, even though sendmail() already
    # succeeded. _with_retry would then resend the same email. Fix: track
    # whether sendmail() itself completed, and treat a cleanup-only failure
    # after that point as a warning, not a reason to retry/resend.
    def _via_465():
        sent = False
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
                server.sendmail(config.GMAIL_ADDRESS, config.RECIPIENT_EMAIL, msg.as_string())
                sent = True
        except Exception:
            if sent:
                print("SMTP:465 sendmail succeeded but QUIT/close failed — not resending", flush=True)
                return
            raise

    def _via_587():
        sent = False
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
                server.sendmail(config.GMAIL_ADDRESS, config.RECIPIENT_EMAIL, msg.as_string())
                sent = True
        except Exception:
            if sent:
                print("SMTP:587 sendmail succeeded but QUIT/close failed — not resending", flush=True)
                return
            raise

    try:
        _with_retry(_via_465, max_attempts=2, label="SMTP:465")
        return
    except Exception as e:
        # Don't swallow silently — an auth failure looks identical to a
        # network blip unless the 465 error is visible somewhere.
        print(f"SMTP:465 failed ({e}), falling back to 587", flush=True)

    try:
        _with_retry(_via_587, max_attempts=2, label="SMTP:587")
        return
    except Exception as e:
        smtp_error = e
        if sys.platform == "win32":
            print(f"SMTP:587 failed ({e}), falling back to Outlook COM", flush=True)
        else:
            print(f"SMTP:587 failed ({e}), falling back to Gmail API (if configured)", flush=True)

    if sys.platform == "win32":
        _send_via_outlook(subject, html)
        print("sent via Outlook COM fallback", flush=True)
        return

    from . import gmail_api

    if not gmail_api.is_configured():
        # Preserve the original SMTP exception type/traceback — callers
        # (and tests) match on smtplib.SMTPException, not a generic wrapper.
        # The "how to fix" guidance still reaches the log via the print
        # above; re-raising here just re-throws what SMTP already raised.
        raise smtp_error

    gmail_api.send_email_via_api(subject, html)
    print("sent via Gmail API fallback (HTTPS/443)", flush=True)


def already_sent_today(subject_contains: str) -> bool:
    """IMAP pre-check: does today's mailbox already contain a message whose
    subject contains this string? This is the cross-machine dedup source of
    truth (SMTP itself has no idempotency) — see mcp-integration plan
    architecture decision 4."""
    today_imap = datetime.now().strftime("%d-%b-%Y")
    # imaplib encodes SEARCH criteria as ASCII — the subject's emoji would
    # raise UnicodeEncodeError on every call (and every retry), turning the
    # dedup check into a guaranteed send failure. Match on the ASCII part.
    ascii_subject = subject_contains.encode("ascii", "ignore").decode().strip()

    def _check():
        with imaplib.IMAP4_SSL("imap.gmail.com", timeout=30) as imap:
            imap.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            imap.select("INBOX", readonly=True)
            typ, data = imap.search(None, f'(SINCE "{today_imap}" SUBJECT "{ascii_subject}")')
            ids = data[0].split() if data and data[0] else []
            return len(ids) > 0

    try:
        return _with_retry(_check, max_attempts=3, label="IMAP check")
    except Exception as e:
        from . import gmail_api

        if not gmail_api.is_configured():
            # Fail closed: if we can't verify "already sent", don't send —
            # a caller catching this exception should treat it as "skip",
            # not "assume not sent and risk a duplicate".
            raise
        print(f"IMAP check failed ({e}), falling back to Gmail API (HTTPS/443)", flush=True)
        return gmail_api.already_sent_today_via_api(subject_contains)


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
