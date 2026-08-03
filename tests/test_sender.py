"""Tests for sender.py: two-part split, IMAP pre-check, retry helper, and
the SMTP -> Outlook COM fallback.

No real network — imaplib.IMAP4_SSL and subprocess.run are mocked,
time.sleep is patched out.
"""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from briefing.sender import (
    _inline,
    _section_style,
    _send_via_outlook,
    _SECTION_STYLES,
    _with_retry,
    already_sent_today,
    markdown_to_html,
    render_social_post_html,
    send_email,
    send_social_post_email,
    send_two_part_briefing,
    split_two_parts,
)


# ---- split_two_parts --------------------------------------------------------


def _markdown_with_n_sections(n):
    sections = [f"## Section {i}\ncontent {i}" for i in range(1, n + 1)]
    return "# AI Briefing\n" + "\n".join(sections)


def test_split_two_parts_splits_at_seventh_section():
    markdown = _markdown_with_n_sections(9)
    part1, part2 = split_two_parts(markdown)

    for i in range(1, 7):
        assert f"Section {i}" in part1
    for i in range(7, 10):
        assert f"Section {i}" in part2
    assert "Section 7" not in part1
    assert "Section 6" not in part2


def test_split_two_parts_part2_has_its_own_heading():
    markdown = _markdown_with_n_sections(8)
    _, part2 = split_two_parts(markdown)
    assert part2.startswith("# AI Briefing — Part 2\n")


def test_split_two_parts_fewer_than_seven_sections_leaves_part2_empty():
    markdown = _markdown_with_n_sections(3)
    part1, part2 = split_two_parts(markdown)
    assert "Section 1" in part1
    assert "Section 3" in part1
    assert part2 == "# AI Briefing — Part 2\n"


# ---- _with_retry -------------------------------------------------------------


def test_with_retry_returns_on_first_success():
    fn = MagicMock(return_value="ok")
    with patch("briefing.sender.time.sleep") as mock_sleep:
        result = _with_retry(fn, max_attempts=3)
    assert result == "ok"
    fn.assert_called_once()
    mock_sleep.assert_not_called()


def test_with_retry_retries_then_succeeds():
    fn = MagicMock(side_effect=[RuntimeError("first fail"), "ok"])
    with patch("briefing.sender.time.sleep") as mock_sleep:
        result = _with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert fn.call_count == 2
    mock_sleep.assert_called_once()


def test_with_retry_exhausts_attempts_then_raises():
    fn = MagicMock(side_effect=RuntimeError("always fails"))
    with patch("briefing.sender.time.sleep") as mock_sleep:
        with pytest.raises(RuntimeError, match="always fails"):
            _with_retry(fn, max_attempts=3)
    assert fn.call_count == 3
    assert mock_sleep.call_count == 2  # slept between attempts, not after the last


# ---- already_sent_today (mailbox pre-check, Gmail API first) ----------------
#
# gmail_api.is_configured is ALWAYS patched here: it checks a real file
# (data/gmail_oauth_token.json) which exists on machines where the one-time
# OAuth setup was done — without pinning it, these tests would take
# different paths (or hit the live API) depending on the machine.


def _mock_imap(search_return_ids: bytes):
    imap = MagicMock()
    imap.__enter__.return_value = imap
    imap.__exit__.return_value = False
    imap.search.return_value = ("OK", [search_return_ids])
    return imap


def _api_unconfigured():
    return patch("briefing.gmail_api.is_configured", return_value=False)


def test_already_sent_today_true_when_message_found():
    imap = _mock_imap(b"101 102")
    with _api_unconfigured(), patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap):
        assert already_sent_today("AI Briefing Part 1") is True
    imap.login.assert_called_once()
    imap.select.assert_called_once_with("INBOX", readonly=True)


def test_already_sent_today_false_when_no_message_found():
    imap = _mock_imap(b"")
    with _api_unconfigured(), patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap):
        assert already_sent_today("AI Briefing Part 1") is False


def test_already_sent_today_short_circuits_without_touching_smtp():
    imap = _mock_imap(b"101")
    with _api_unconfigured(), patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap) as mock_ssl:
        already_sent_today("AI Briefing Part 1")
    mock_ssl.assert_called_once_with("imap.gmail.com", timeout=30)


def test_already_sent_today_retries_transient_failure_then_succeeds():
    good_imap = _mock_imap(b"101")
    with _api_unconfigured(), patch(
        "briefing.sender.imaplib.IMAP4_SSL",
        side_effect=[ConnectionResetError("reset"), good_imap],
    ), patch("briefing.sender.time.sleep"):
        assert already_sent_today("AI Briefing Part 1") is True


def test_already_sent_today_uses_gmail_api_first_without_touching_imap():
    with patch("briefing.gmail_api.is_configured", return_value=True), \
         patch("briefing.gmail_api.already_sent_today_via_api", return_value=True) as mock_api, \
         patch("briefing.sender.imaplib.IMAP4_SSL") as mock_imap:
        assert already_sent_today("AI Briefing Part 1") is True
    mock_api.assert_called_once_with("AI Briefing Part 1")
    mock_imap.assert_not_called()


def test_already_sent_today_falls_back_to_imap_when_gmail_api_fails():
    imap = _mock_imap(b"101")
    with patch("briefing.gmail_api.is_configured", return_value=True), \
         patch("briefing.gmail_api.already_sent_today_via_api", side_effect=RuntimeError("api down")), \
         patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap), \
         patch("briefing.sender.time.sleep"):
        assert already_sent_today("AI Briefing Part 1") is True


def test_already_sent_today_raises_when_all_transports_exhausted():
    # Fail closed: no transport could verify -> raise, never "assume unsent".
    with patch(
        "briefing.sender.imaplib.IMAP4_SSL",
        side_effect=ConnectionResetError("reset"),
    ), _api_unconfigured(), patch("briefing.sender.time.sleep"):
        with pytest.raises(ConnectionResetError):
            already_sent_today("AI Briefing Part 1")


# ---- multi-recipient To handling --------------------------------------------


def test_send_email_smtp_delivers_to_all_recipients():
    server = MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    recipients = ["a@example.com", "b@example.com"]
    with _api_unconfigured(), \
         patch("briefing.sender.config.RECIPIENT_EMAILS", recipients), \
         patch("briefing.sender.smtplib.SMTP_SSL", return_value=server):
        send_email("subject", "<p>html</p>")
    # sendmail must get the LIST — a comma-joined string would deliver to
    # only the first address.
    args = server.sendmail.call_args[0]
    assert args[1] == recipients


# ---- send_email: Gmail API primary -> SMTP -> Outlook COM (win32) -----------


def _mock_smtp_ssl_failing():
    return patch("briefing.sender.smtplib.SMTP_SSL", side_effect=smtplib.SMTPException("465 blocked"))


def _mock_smtp_failing():
    return patch("briefing.sender.smtplib.SMTP", side_effect=smtplib.SMTPException("587 blocked"))


def test_send_email_does_not_attempt_outlook_on_darwin():
    with patch("briefing.sender.sys.platform", "darwin"), \
         _mock_smtp_ssl_failing(), _mock_smtp_failing(), \
         patch("briefing.sender.subprocess.run") as mock_run, \
         patch("briefing.gmail_api.is_configured", return_value=False), \
         patch("briefing.sender.time.sleep"):
        with pytest.raises(smtplib.SMTPException):
            send_email("subject", "<p>html</p>")
    mock_run.assert_not_called()


def test_send_email_uses_gmail_api_first_without_touching_smtp():
    with patch("briefing.gmail_api.is_configured", return_value=True), \
         patch("briefing.gmail_api.send_email_via_api") as mock_api_send, \
         patch("briefing.sender.smtplib.SMTP_SSL") as mock_smtp:
        send_email("subject", "<p>html</p>")
    mock_api_send.assert_called_once_with("subject", "<p>html</p>")
    mock_smtp.assert_not_called()


def test_send_email_falls_back_to_smtp_when_gmail_api_fails():
    server = MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    with patch("briefing.gmail_api.is_configured", return_value=True), \
         patch("briefing.gmail_api.send_email_via_api", side_effect=RuntimeError("api down")), \
         patch("briefing.sender.smtplib.SMTP_SSL", return_value=server), \
         patch("briefing.sender.time.sleep"):
        send_email("subject", "<p>html</p>")
    server.sendmail.assert_called_once()


def _mock_smtp_ssl_sendmail_ok_but_quit_fails():
    """Reproduces the real bug found 2026-07-25: sendmail() succeeds but the
    connection resets during __exit__'s QUIT, which smtplib only swallows
    for SMTPServerDisconnected — not ConnectionResetError. Without the
    sent-flag fix, this exception looked identical to a failed send and
    _with_retry resent the same email, causing 2 real duplicate sends
    (4 emails delivered for what should have been 2)."""
    server = MagicMock()
    server.sendmail = MagicMock()  # succeeds
    server.__enter__.return_value = server
    server.__exit__.side_effect = ConnectionResetError("reset during QUIT")
    return patch("briefing.sender.smtplib.SMTP_SSL", return_value=server), server


def test_send_email_does_not_resend_when_only_smtp_quit_fails():
    ctx, server = _mock_smtp_ssl_sendmail_ok_but_quit_fails()
    with ctx, _api_unconfigured(), patch("briefing.sender.time.sleep"):
        send_email("subject", "<p>html</p>")
    server.sendmail.assert_called_once()  # not retried/resent


def test_send_email_raises_original_smtp_error_when_gmail_api_unconfigured():
    with patch("briefing.sender.sys.platform", "darwin"), \
         _mock_smtp_ssl_failing(), _mock_smtp_failing(), \
         patch("briefing.gmail_api.is_configured", return_value=False), \
         patch("briefing.sender.time.sleep"):
        with pytest.raises(smtplib.SMTPException, match="587 blocked"):
            send_email("subject", "<p>html</p>")


def test_send_email_falls_back_to_outlook_on_win32_when_smtp_fails():
    with patch("briefing.sender.sys.platform", "win32"), \
         _mock_smtp_ssl_failing(), _mock_smtp_failing(), _api_unconfigured(), \
         patch("briefing.sender.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
         patch("briefing.sender.time.sleep"):
        send_email("subject", "<p>html</p>")
    mock_run.assert_called_once()


def test_send_via_outlook_uses_temp_file_not_inline_html():
    html = "<p>secret payload that must never hit the shell</p>"
    with patch("briefing.sender.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
         patch("briefing.sender.os.remove") as mock_remove:
        _send_via_outlook("subject", html)

    args, kwargs = mock_run.call_args
    command = args[0]
    ps_script = command[-1]
    assert html not in ps_script
    assert "ReadAllText" in ps_script
    mock_remove.assert_called_once()


def test_send_via_outlook_escapes_single_quotes_in_subject():
    with patch("briefing.sender.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
         patch("briefing.sender.os.remove"):
        _send_via_outlook("Bob's briefing", "<p>html</p>")

    ps_script = mock_run.call_args[0][0][-1]
    assert "Bob''s briefing" in ps_script


def test_send_via_outlook_raises_on_nonzero_returncode():
    with patch(
        "briefing.sender.subprocess.run",
        return_value=MagicMock(returncode=1, stderr="COM error"),
    ), patch("briefing.sender.os.remove"):
        with pytest.raises(RuntimeError, match="COM error"):
            _send_via_outlook("subject", "<p>html</p>")


# ---- _inline (markdown -> inline HTML) --------------------------------------


def test_inline_renders_markdown_link_as_anchor():
    html = _inline("[Anthropic](https://anthropic.com)")
    assert html == '<a href="https://anthropic.com" style="color:#26890D">Anthropic</a>'


def test_inline_leaves_non_http_link_as_plain_label():
    html = _inline("[label](ftp://example.com/file)")
    assert html == "label"
    assert "<a" not in html


def test_inline_renders_bold_as_strong():
    html = _inline("**important**")
    assert html == "<strong>important</strong>"


def test_inline_renders_italic_as_em():
    html = _inline("*aside*")
    assert html == "<em>aside</em>"


def test_inline_composes_bold_and_link_without_corruption():
    html = _inline("**Big News**: read the [full report](https://example.com/report)")
    assert "<strong>Big News</strong>" in html
    assert '<a href="https://example.com/report" style="color:#26890D">full report</a>' in html


# ---- _section_style ----------------------------------------------------------


def test_section_style_matches_top_3_header():
    assert _section_style("Top 3 Stories Today") == _SECTION_STYLES["top 3"]


def test_section_style_matches_security_header():
    assert _section_style("AI Security & Privacy Roundup") == _SECTION_STYLES["security"]


def test_section_style_matches_community_header():
    assert _section_style("Community Highlights") == _SECTION_STYLES["community"]


# ---- markdown_to_html: individual parsing branches --------------------------


def test_markdown_to_html_renders_story_card_headline():
    md = "## News\n**Big Announcement** Something happened today.\n"
    out = markdown_to_html(md, "31 Jul 2026")
    assert "<h3" in out
    assert "Big Announcement" in out
    assert "Something happened today." in out


def test_markdown_to_html_renders_callout_box():
    md = (
        "## News\n"
        "**Big Announcement**\n"
        "**Key takeaway:** This changes everything.\n"
    )
    out = markdown_to_html(md, "31 Jul 2026")
    assert "Key takeaway" in out
    assert "This changes everything." in out
    assert "border-left:3px solid" in out


def test_markdown_to_html_renders_social_post_box():
    md = (
        "## News\n"
        "**Big Announcement**\n"
        "📱 Social post: Check this out!\n"
    )
    out = markdown_to_html(md, "31 Jul 2026")
    assert "SHARE-READY POST" in out
    assert "Check this out!" in out


def test_markdown_to_html_renders_source_link_line():
    md = (
        "## News\n"
        "**Big Announcement** Here is the story.\n"
        "Source: https://example.com/article\n"
    )
    out = markdown_to_html(md, "31 Jul 2026")
    assert "🔗" in out
    assert "https://example.com/article" in out


def test_markdown_to_html_renders_divider_as_rule():
    md = "## News\nSome intro text.\n---\n## More\nSome other text.\n"
    out = markdown_to_html(md, "31 Jul 2026")
    assert "border-top:2px solid #86BC25;" in out


def test_markdown_to_html_composes_multiple_sections_without_corruption():
    md = (
        "## Top 3\n"
        "**OpenAI Ships New Model** OpenAI released a major update today.\n"
        "**Key takeaway:** This changes the competitive landscape.\n"
        "📱 Social post: Big day for AI!\n"
        "Source: https://openai.com/blog/update\n"
        "---\n"
        "## Community\n"
        "Members shared their favorite tools this week.\n"
    )
    out = markdown_to_html(md, "31 Jul 2026")
    assert "🔥 Top Stories" in out
    assert "💬 Community" in out
    assert "OpenAI Ships New Model" in out
    assert "Key takeaway" in out
    assert "SHARE-READY POST" in out
    assert "🔗" in out
    assert "border-top:2px solid #86BC25;" in out


# ---- send_email: SMTP:465 fails, SMTP:587 recovers ---------------------------


def test_send_email_recovers_via_smtp_587_when_465_fails():
    server = MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    with _api_unconfigured(), _mock_smtp_ssl_failing(), \
         patch("briefing.sender.smtplib.SMTP", return_value=server), \
         patch("briefing.sender.time.sleep"):
        send_email("subject", "<p>html</p>")
    server.starttls.assert_called_once()
    server.sendmail.assert_called_once()


# ---- send_two_part_briefing --------------------------------------------------


def _mock_send_lock():
    cm = MagicMock()
    cm.__enter__.return_value = None
    cm.__exit__.return_value = False
    return patch("briefing.db.send_lock", return_value=cm)


def test_send_two_part_briefing_both_parts_sent():
    with patch("briefing.sender.already_sent_today", return_value=False), \
         patch("briefing.sender.send_email") as mock_send, \
         _mock_send_lock():
        result = send_two_part_briefing("<p>p1</p>", "<p>p2</p>", "31 Jul 2026")
    assert result == {"part1": "sent", "part2": "sent"}
    assert mock_send.call_count == 2


def test_send_two_part_briefing_skips_part_already_sent():
    with patch("briefing.sender.already_sent_today", side_effect=[True, False]), \
         patch("briefing.sender.send_email") as mock_send, \
         _mock_send_lock():
        result = send_two_part_briefing("<p>p1</p>", "<p>p2</p>", "31 Jul 2026")
    assert result["part1"] == "already_sent"
    assert result["part2"] == "sent"
    mock_send.assert_called_once()


def test_send_two_part_briefing_partial_when_one_part_fails():
    def _send_side_effect(subject, html):
        if "Part 2" in subject:
            raise RuntimeError("smtp blip")

    with patch("briefing.sender.already_sent_today", return_value=False), \
         patch("briefing.sender.send_email", side_effect=_send_side_effect), \
         _mock_send_lock():
        result = send_two_part_briefing("<p>p1</p>", "<p>p2</p>", "31 Jul 2026")
    assert result["part1"] == "sent"
    assert result["part2"] == "error: smtp blip"


def test_send_two_part_briefing_both_error_when_both_fail():
    with patch("briefing.sender.already_sent_today", return_value=False), \
         patch("briefing.sender.send_email", side_effect=RuntimeError("down")), \
         _mock_send_lock():
        result = send_two_part_briefing("<p>p1</p>", "<p>p2</p>", "31 Jul 2026")
    assert result == {"part1": "error: down", "part2": "error: down"}


# ---- render_social_post_html -------------------------------------------------


def test_render_social_post_html_escapes_and_preserves_line_breaks():
    html = render_social_post_html("Line one\nLine <script>two</script>", "31 Jul 2026")
    assert "Line one<br>\nLine &lt;script&gt;two&lt;/script&gt;" in html
    assert "<script>" not in html


def test_render_social_post_html_includes_date():
    html = render_social_post_html("post body", "31 Jul 2026")
    assert "31 Jul 2026" in html


# ---- send_social_post_email --------------------------------------------------


def test_send_social_post_email_sent():
    with patch("briefing.sender.already_sent_today", return_value=False), \
         patch("briefing.sender.send_email") as mock_send, \
         _mock_send_lock():
        result = send_social_post_email("<p>post</p>", "31 Jul 2026")
    assert result == "sent"
    mock_send.assert_called_once()
    subject_arg = mock_send.call_args.args[0]
    assert "31 Jul 2026" in subject_arg
    assert "Social Post" in subject_arg


def test_send_social_post_email_already_sent():
    with patch("briefing.sender.already_sent_today", return_value=True), \
         patch("briefing.sender.send_email") as mock_send, \
         _mock_send_lock():
        result = send_social_post_email("<p>post</p>", "31 Jul 2026")
    assert result == "already_sent"
    mock_send.assert_not_called()


def test_send_social_post_email_error_on_send_failure():
    with patch("briefing.sender.already_sent_today", return_value=False), \
         patch("briefing.sender.send_email", side_effect=RuntimeError("smtp down")), \
         _mock_send_lock():
        result = send_social_post_email("<p>post</p>", "31 Jul 2026")
    assert result == "error: smtp down"


def test_send_social_post_email_marker_distinct_from_briefing_parts():
    # already_sent_today's substring match must not conflate this send with
    # the daily Part 1/Part 2 emails — a shared marker would make the dedup
    # check skip the social post because a Part 1/2 email already went out.
    with patch("briefing.sender.already_sent_today") as mock_check, \
         patch("briefing.sender.send_email"), \
         _mock_send_lock():
        mock_check.return_value = False
        send_social_post_email("<p>post</p>", "31 Jul 2026")
    marker_arg = mock_check.call_args.args[0]
    assert "Part 1" not in marker_arg
    assert "Part 2" not in marker_arg
