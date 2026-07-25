"""Tests for sender.py: two-part split, IMAP pre-check, retry helper, and
the SMTP -> Outlook COM fallback.

No real network — imaplib.IMAP4_SSL and subprocess.run are mocked,
time.sleep is patched out.
"""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from briefing.sender import (
    _send_via_outlook,
    _with_retry,
    already_sent_today,
    send_email,
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
