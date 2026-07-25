"""Tests for generator.py: prompt/context assembly, the char budget, and the
maxplus -> Gemini-direct provider chain (with mocked urllib, no network).
"""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

from briefing import config
from briefing.generator import (
    TOTAL_CHAR_BUDGET,
    TRANSCRIPT_CHAR_CAP,
    _cap_transcript,
    _claude_cli_call,
    _grok_call,
    _rendered_size,
    _sanitize,
    budget_items,
    build_context,
)


def _item(title="T", url="U", content="", source_type="news"):
    return {"title": title, "url": url, "content": content, "source_type": source_type}


# ---- _cap_transcript (8000-char transcript cap) ---------------------------


def test_cap_transcript_truncates_long_youtube_content():
    item = _item(content="a" * 20_000, source_type="youtube")
    capped = _cap_transcript(item)
    assert len(capped["content"]) < 20_000
    assert "[transcript truncated]" in capped["content"]
    half = TRANSCRIPT_CHAR_CAP // 2
    assert capped["content"].startswith("a" * half)
    assert capped["content"].endswith("a" * half)


def test_cap_transcript_leaves_short_youtube_content_untouched():
    item = _item(content="short transcript", source_type="youtube")
    assert _cap_transcript(item) == item


def test_cap_transcript_ignores_non_youtube_long_content():
    item = _item(content="a" * 20_000, source_type="news")
    capped = _cap_transcript(item)
    assert capped["content"] == item["content"]


def test_cap_transcript_does_not_mutate_input():
    original_content = "a" * 20_000
    item = _item(content=original_content, source_type="youtube")
    _cap_transcript(item)
    assert item["content"] == original_content


# ---- budget_items: 60000-char input budget enforced ------------------------


def test_budget_items_keeps_everything_under_budget():
    items = [_item(content="x" * 100) for _ in range(5)]
    kept = budget_items(items)
    assert len(kept) == 5


def test_budget_items_enforces_total_char_budget():
    # Each item's rendered size is ~2014 chars (content sliced to 2000 by
    # _rendered_size); 35 of them would total ~70k, over the 60k budget.
    items = [_item(title="T", url="U", content="x" * 3000, source_type="news") for _ in range(35)]
    kept = budget_items(items)

    assert len(kept) < len(items)
    total = sum(_rendered_size(item) for item in kept)
    assert total <= TOTAL_CHAR_BUDGET


def test_budget_items_respects_extra_budget_used():
    # Same items that fully fit under the full 60k budget...
    items = [_item(title="T", url="U", content="x" * 3000, source_type="news") for _ in range(20)]
    assert len(budget_items(items)) == 20

    # ...but not once research findings have already eaten most of the budget.
    kept = budget_items(items, extra_budget_used=TOTAL_CHAR_BUDGET - 5_000)
    assert len(kept) < 20


def test_budget_items_drops_whole_items_not_partial_content():
    items = [_item(title="T", url="U", content="x" * 3000, source_type="news") for _ in range(35)]
    kept = budget_items(items)
    for item in kept:
        # content is untouched (still the full 3000 chars) — dropping is
        # whole-item, never a mid-item truncation.
        assert len(item["content"]) == 3000


# ---- budget_items: whole-item drop by priority -----------------------------


def test_budget_items_prioritizes_research_over_youtube():
    # A late-in-list "research" item (lowest SOURCE_PRIORITY number = kept
    # last-dropped) must survive even though many higher-index "youtube"
    # items (highest SOURCE_PRIORITY number = dropped first) come before it.
    youtube_items = [
        _item(title="Y", url="U", content="x" * 3000, source_type="youtube")
        for _ in range(35)
    ]
    research_item = _item(title="R", url="U", content="x" * 3000, source_type="research")
    items = youtube_items + [research_item]

    kept = budget_items(items)

    assert research_item in kept
    assert len(kept) < len(items)
    dropped = [i for i in items if i not in kept]
    assert all(i["source_type"] == "youtube" for i in dropped)


def test_budget_items_respects_max_items_cap():
    items = [_item(title=f"T{i}", content="x" * 10) for i in range(10)]
    kept = budget_items(items, max_items=3)
    assert len(kept) == 3


# ---- build_context: markdown links -----------------------------------------


def test_build_context_emits_markdown_links():
    items = [_item(title="Cool Story", url="https://example.com/story", content="body")]
    context = build_context(items)
    assert "[Cool Story](https://example.com/story)" in context


def test_build_context_omits_link_syntax_when_no_url():
    items = [_item(title="No URL Item", url="", content="body")]
    context = build_context(items)
    assert "- No URL Item" in context
    assert "[No URL Item]" not in context


def test_build_context_groups_by_source_type():
    items = [
        _item(title="News Item", source_type="news"),
        _item(title="GH Item", source_type="github"),
    ]
    context = build_context(items)
    assert "=== NEWS ===" in context
    assert "=== GITHUB ===" in context


# ---- _sanitize: preserves markdown links, strips bare URLs ----------------


def test_sanitize_preserves_markdown_links():
    text = "Read more: [Cool Story](https://example.com/story) for details."
    sanitized = _sanitize(text)
    assert "[Cool Story](https://example.com/story)" in sanitized


def test_sanitize_strips_bare_urls():
    text = "See https://bare.example.com/page for more."
    sanitized = _sanitize(text)
    assert "https://bare.example.com/page" not in sanitized


def test_sanitize_masks_security_terms():
    text = "Researchers found a new exploit and a way to bypass auth."
    sanitized = _sanitize(text)
    assert "exploit" not in sanitized.lower()
    assert "bypass" not in sanitized.lower()
    assert "[security-related]" in sanitized


def test_sanitize_bare_url_and_link_together():
    text = "[Source](https://good.example.com/a) but also see https://bare.example.com/b directly."
    sanitized = _sanitize(text)
    assert "[Source](https://good.example.com/a)" in sanitized
    assert "https://bare.example.com/b" not in sanitized


# ---- _grok_call: maxplus -> Gemini-direct provider chain -------------------


def _http_error(code, body=b'{"error": "boom"}'):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def _ok_response(payload):
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    resp.__exit__.return_value = False
    return resp


MAXPLUS_OK = {"choices": [{"message": {"content": "maxplus reply"}}]}
GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "gemini reply"}]}}]}


def test_402_no_retry_immediate_fallback_to_gemini():
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", "g-key"), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep") as sleep:
        urlopen.side_effect = [_http_error(402, b'{"error": "insufficient_credit"}'), _ok_response(GEMINI_OK)]
        result = _grok_call("sys", "user", max_attempts=4)

    assert result == "gemini reply"
    assert urlopen.call_count == 2  # one fail-fast maxplus attempt, one gemini attempt
    sleep.assert_not_called()


def test_503_retries_before_succeeding():
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", ""), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep") as sleep:
        urlopen.side_effect = [_http_error(503), _http_error(503), _ok_response(MAXPLUS_OK)]
        result = _grok_call("sys", "user", max_attempts=4)

    assert result == "maxplus reply"
    assert urlopen.call_count == 3
    assert sleep.call_count == 2


def test_both_providers_fail_raises():
    # Claude CLI is the 3rd fallback tier (see _claude_cli_call) — disabled
    # here so a real `claude` binary on the test machine's PATH can't mask
    # the HTTP-tier failure this test is asserting on.
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", "g-key"), \
         patch.object(config, "CLAUDE_CLI_ENABLED", False), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep"):
        urlopen.side_effect = [_http_error(503), _http_error(503), _http_error(500), _http_error(500)]
        try:
            _grok_call("sys", "user", max_attempts=2)
            assert False, "expected an exception"
        except urllib.error.HTTPError as e:
            assert e.code == 500  # last provider's (Gemini's) error


def test_no_gemini_api_key_raises_maxplus_error():
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", ""), \
         patch.object(config, "CLAUDE_CLI_ENABLED", False), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep"):
        urlopen.side_effect = [_http_error(402, b'{"error": "insufficient_credit"}')]
        try:
            _grok_call("sys", "user", max_attempts=4)
            assert False, "expected an exception"
        except urllib.error.HTTPError as e:
            assert e.code == 402

    assert urlopen.call_count == 1  # no Gemini fallback attempted


# ---- _claude_cli_call: subprocess.run("claude -p ...") --------------------


def _cli_result(returncode=0, stdout="claude reply", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_claude_cli_call_returns_stdout_on_success():
    with patch("briefing.generator.subprocess.run", return_value=_cli_result()) as run:
        result = _claude_cli_call("sys", "user")

    assert result == "claude reply"
    args, kwargs = run.call_args
    assert args[0] == ["claude", "-p", "--model", config.CLAUDE_CLI_MODEL, "-"]
    assert kwargs["input"] == "sys\n\nuser"
    assert kwargs["timeout"] == 300


def test_claude_cli_call_raises_on_nonzero_exit():
    with patch("briefing.generator.subprocess.run", return_value=_cli_result(returncode=1, stdout="", stderr="boom")):
        try:
            _claude_cli_call("sys", "user")
            assert False, "expected an exception"
        except RuntimeError as e:
            assert "boom" in str(e)


def test_claude_cli_call_raises_on_empty_stdout():
    with patch("briefing.generator.subprocess.run", return_value=_cli_result(returncode=0, stdout="   ")):
        try:
            _claude_cli_call("sys", "user")
            assert False, "expected an exception"
        except RuntimeError:
            pass


# ---- _grok_call: full chain including the Claude CLI safety net -----------


def test_claude_cli_tier_skipped_when_not_installed():
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", "g-key"), \
         patch.object(config, "CLAUDE_CLI_ENABLED", True), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("briefing.generator.shutil.which", return_value=None), \
         patch("briefing.generator.subprocess.run") as cli_run, \
         patch("time.sleep"):
        urlopen.side_effect = [
            _http_error(402, b'{"error": "insufficient_credit"}'),
            _http_error(429),
            _http_error(429),
        ]
        try:
            _grok_call("sys", "user", max_attempts=2)
            assert False, "expected an exception"
        except urllib.error.HTTPError:
            pass

    cli_run.assert_not_called()


def test_full_chain_falls_through_to_claude_cli():
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch.object(config, "GEMINI_API_KEY", "g-key"), \
         patch.object(config, "CLAUDE_CLI_ENABLED", True), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("briefing.generator.shutil.which", return_value="/usr/local/bin/claude"), \
         patch("briefing.generator.subprocess.run", return_value=_cli_result(stdout="claude reply")), \
         patch("time.sleep"):
        urlopen.side_effect = [
            _http_error(402, b'{"error": "insufficient_credit"}'),  # maxplus fails fast
            _http_error(429),  # gemini exhausts retries
            _http_error(429),
        ]
        result = _grok_call("sys", "user", max_attempts=2)

    assert result == "claude reply"
