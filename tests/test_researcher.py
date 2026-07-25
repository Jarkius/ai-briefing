"""Tests for researcher.py: request-line parsing and input-type routing.

No network, no real MCP server — session.call_tool is an AsyncMock.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from briefing.researcher import _public_url_error, classify, parse_requests, research_one


def _allow_all_urls():
    """Bypass the SSRF guard's real DNS lookup — this suite runs no-network."""
    return patch("briefing.researcher._public_url_error", return_value=None)


def _text_result(text: str):
    """Fake an MCP call_tool() result shaped like mcp_client.tool_text expects."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


# ---- parse_requests -------------------------------------------------------


def test_parse_requests_unchecked_line():
    reqs = parse_requests("- [ ] look into diffusion models")
    assert reqs == [{"line_index": 0, "checked": False, "text": "look into diffusion models"}]


def test_parse_requests_checked_line():
    reqs = parse_requests("- [x] already done")
    assert reqs == [{"line_index": 0, "checked": True, "text": "already done"}]


def test_parse_requests_mixed_lines_and_noise():
    text = "\n".join([
        "# Research requests",
        "- [ ] first pending",
        "some prose line, not a checkbox",
        "- [x] second done (researched 2026-07-20)",
        "- [ ] third pending",
    ])
    reqs = parse_requests(text)
    assert [r["text"] for r in reqs] == [
        "first pending",
        "second done (researched 2026-07-20)",
        "third pending",
    ]
    assert [r["checked"] for r in reqs] == [False, True, False]
    # line_index tracks the original line number, not the filtered position
    assert [r["line_index"] for r in reqs] == [1, 3, 4]


def test_parse_requests_no_checkboxes():
    assert parse_requests("just some text\nno checkboxes here") == []


# ---- classify (routing decision) -----------------------------------------


def test_classify_youtube_watch_url():
    assert classify("check out https://www.youtube.com/watch?v=abc123") == "youtube"


def test_classify_youtube_short_url():
    assert classify("https://youtu.be/abc123") == "youtube"


def test_classify_other_url():
    assert classify("read https://example.com/some-article") == "url"


def test_classify_bare_topic():
    assert classify("prompt engineering best practices") == "topic"


# ---- research_one routing (mocked session) --------------------------------


def test_research_one_youtube_calls_transcribe_video():
    session = AsyncMock()
    session.call_tool.return_value = _text_result("transcript text")

    with _allow_all_urls():
        result = asyncio.run(research_one(session, "https://youtu.be/abc123"))

    session.call_tool.assert_awaited_once_with(
        "transcribe_video", {"url": "https://youtu.be/abc123"}
    )
    assert "transcript text" in result


def test_research_one_url_calls_visit_page():
    session = AsyncMock()
    session.call_tool.return_value = _text_result("page content")

    with _allow_all_urls():
        result = asyncio.run(research_one(session, "https://example.com/article"))

    session.call_tool.assert_awaited_once_with(
        "visit_page", {"url": "https://example.com/article"}
    )
    assert "page content" in result


def test_research_one_topic_calls_search_feeds_then_google_search():
    session = AsyncMock()
    session.call_tool.side_effect = [
        _text_result("feed findings"),
        _text_result("web findings"),
    ]

    result = asyncio.run(research_one(session, "prompt injection defenses"))

    assert session.call_tool.await_count == 2
    first_call, second_call = session.call_tool.await_args_list
    assert first_call.args[0] == "search_feeds"
    assert first_call.args[1] == {"query": "prompt injection defenses", "limit": 5}
    assert second_call.args[0] == "google_search"
    assert second_call.args[1] == {"query": "prompt injection defenses", "num_results": 5}
    assert "feed findings" in result
    assert "web findings" in result


def test_research_one_url_failure_is_caught_not_raised():
    session = AsyncMock()
    session.call_tool.side_effect = RuntimeError("boom")

    with _allow_all_urls():
        result = asyncio.run(research_one(session, "https://example.com/article"))

    assert "page fetch failed" in result
    assert "boom" in result


def test_research_one_topic_google_blocked_is_noted_not_raised():
    session = AsyncMock()
    session.call_tool.side_effect = [
        _text_result("feed findings"),
        _text_result("Our systems detected unusual traffic — blocked"),
    ]

    result = asyncio.run(research_one(session, "some topic"))

    assert "blocked by bot detection" in result


# ---- SSRF guard (_public_url_error) ------------------------------------------
# Loopback resolution needs no real DNS, so these run offline.


def test_ssrf_guard_refuses_loopback():
    assert _public_url_error("http://127.0.0.1:8787/admin") is not None


def test_ssrf_guard_refuses_localhost_name():
    assert _public_url_error("http://localhost/") is not None


def test_ssrf_guard_refuses_metadata_ip():
    assert _public_url_error("http://169.254.169.254/latest/meta-data/") is not None


def test_ssrf_guard_refuses_private_range():
    assert _public_url_error("http://192.168.1.1/") is not None


def test_research_one_refuses_private_url_without_calling_tool():
    session = AsyncMock()
    result = asyncio.run(research_one(session, "http://127.0.0.1:8787/steal"))
    session.call_tool.assert_not_awaited()
    assert "refused" in result


def test_ssrf_guard_allows_public_ip():
    # 1.1.1.1 is a literal global IP — no DNS needed
    assert _public_url_error("https://1.1.1.1/") is None
