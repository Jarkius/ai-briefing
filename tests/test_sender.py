"""Tests for sender.py: two-part split, IMAP pre-check, and retry helper.

No real network — imaplib.IMAP4_SSL is mocked, time.sleep is patched out.
"""

from unittest.mock import MagicMock, patch

import pytest

from briefing.sender import _with_retry, already_sent_today, split_two_parts


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


# ---- already_sent_today (IMAP pre-check) ------------------------------------


def _mock_imap(search_return_ids: bytes):
    imap = MagicMock()
    imap.__enter__.return_value = imap
    imap.__exit__.return_value = False
    imap.search.return_value = ("OK", [search_return_ids])
    return imap


def test_already_sent_today_true_when_message_found():
    imap = _mock_imap(b"101 102")
    with patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap):
        assert already_sent_today("AI Briefing Part 1") is True
    imap.login.assert_called_once()
    imap.select.assert_called_once_with("INBOX", readonly=True)


def test_already_sent_today_false_when_no_message_found():
    imap = _mock_imap(b"")
    with patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap):
        assert already_sent_today("AI Briefing Part 1") is False


def test_already_sent_today_short_circuits_without_touching_smtp():
    imap = _mock_imap(b"101")
    with patch("briefing.sender.imaplib.IMAP4_SSL", return_value=imap) as mock_ssl:
        already_sent_today("AI Briefing Part 1")
    mock_ssl.assert_called_once_with("imap.gmail.com", timeout=30)


def test_already_sent_today_retries_transient_failure_then_succeeds():
    good_imap = _mock_imap(b"101")
    with patch(
        "briefing.sender.imaplib.IMAP4_SSL",
        side_effect=[ConnectionResetError("reset"), good_imap],
    ), patch("briefing.sender.time.sleep"):
        assert already_sent_today("AI Briefing Part 1") is True
