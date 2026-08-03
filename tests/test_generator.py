"""Tests for generator.py: prompt/context assembly, the char budget, and the
maxplus -> Gemini-direct provider chain (with mocked urllib, no network).
"""

import functools
import io
import json
import sqlite3
import urllib.error
from unittest.mock import MagicMock, patch

import httpx
import pytest

from briefing import config
from briefing.generator import (
    SECTION_PROMPTS,
    SOCIAL_POST_STYLE_ANGLES,
    TOTAL_CHAR_BUDGET,
    TRANSCRIPT_CHAR_CAP,
    _bedrock_call,
    _cap_transcript,
    _claude_cli_call,
    _gemini_call,
    _grok_call,
    _rendered_size,
    _sanitize,
    budget_items,
    build_context,
    build_social_post_source,
    call_gemini,
    fetch_recent_items,
    generate,
    generate_social_post,
    social_post_candidate_items,
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


# ---- _grok_call: configurable provider chain --------------------------------

import pytest


@pytest.fixture(autouse=True)
def _no_real_providers():
    """Pin the provider chain for every test in this module. Without this,
    the bedrock tier (enabled by default, real AWS creds on dev machines)
    would make LIVE API calls from unit tests — observed: tests received
    actual 'Hello! How can I help you today?' responses from Bedrock."""
    with patch.object(config, "BEDROCK_ENABLED", False), \
         patch.object(config, "PROVIDER_ORDER", ["maxplus", "gemini", "claude-cli"]):
        yield


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


# ---- date_str formatting (Windows compatibility) ----------------------------


def test_date_str_avoids_platform_specific_strftime():
    # %-d is a glibc/BSD extension — Windows CPython raises ValueError on it,
    # which soft-failed the whole Generate phase on the Windows machine (no
    # email, silently, every day). Regression guard: the generate() source
    # must not use %-d or %#d anywhere.
    import inspect
    import re

    from briefing import generator

    src = inspect.getsource(generator)
    # Only flag %-d/%#d inside strftime(...) calls — the explanatory comment
    # in generate() legitimately names the forbidden directives.
    offenders = re.findall(r"strftime\([^)]*%[-#]d", src)
    assert offenders == []


def test_claude_cli_tier_retries_transient_failures():
    # The CLI is the LAST tier — a single transient exit-1 must not kill the
    # whole 5am run (observed 2026-07-26 10:17). Two failures then success.
    with patch.object(config, "MAXPLUS_API_KEY", ""), \
         patch.object(config, "GEMINI_API_KEY", ""), \
         patch.object(config, "CLAUDE_CLI_ENABLED", True), \
         patch("briefing.generator._claude_cli_available", return_value=True), \
         patch("briefing.generator._claude_cli_call",
               side_effect=[RuntimeError("blip 1"), RuntimeError("blip 2"), "recovered"]) as cli, \
         patch("briefing.generator.time.sleep") as sleep:
        result = _grok_call("sys", "user")

    assert result == "recovered"
    assert cli.call_count == 3
    assert sleep.call_count == 2


def test_claude_cli_tier_raises_after_three_failures():
    with patch.object(config, "MAXPLUS_API_KEY", ""), \
         patch.object(config, "GEMINI_API_KEY", ""), \
         patch.object(config, "CLAUDE_CLI_ENABLED", True), \
         patch("briefing.generator._claude_cli_available", return_value=True), \
         patch("briefing.generator._claude_cli_call", side_effect=RuntimeError("persistent")), \
         patch("briefing.generator.time.sleep"):
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="persistent"):
            _grok_call("sys", "user")


# ---- provider order configurability -----------------------------------------


def test_provider_order_respected():
    calls = []
    with patch.object(config, "PROVIDER_ORDER", ["gemini", "maxplus"]), \
         patch.object(config, "GEMINI_API_KEY", "g"), \
         patch.object(config, "MAXPLUS_API_KEY", "m"), \
         patch("briefing.generator._gemini_call", side_effect=lambda *a: calls.append("gemini") or (_ for _ in ()).throw(RuntimeError("g down"))), \
         patch("briefing.generator._maxplus_call", side_effect=lambda *a: calls.append("maxplus") or "maxplus reply"):
        result = _grok_call("sys", "user")
    assert result == "maxplus reply"
    assert calls == ["gemini", "maxplus"]


def test_unknown_provider_name_skipped_not_fatal():
    with patch.object(config, "PROVIDER_ORDER", ["carrier-pigeon", "gemini"]), \
         patch.object(config, "GEMINI_API_KEY", "g"), \
         patch("briefing.generator._gemini_call", return_value="gemini reply"):
        assert _grok_call("sys", "user") == "gemini reply"


def test_bedrock_first_when_ordered():
    with patch.object(config, "PROVIDER_ORDER", ["bedrock", "gemini"]), \
         patch.object(config, "BEDROCK_ENABLED", True), \
         patch.object(config, "GEMINI_API_KEY", "g"), \
         patch("briefing.generator._bedrock_call", return_value="bedrock reply") as bcall, \
         patch("briefing.generator._gemini_call") as gcall:
        assert _grok_call("sys", "user") == "bedrock reply"
    bcall.assert_called_once()
    gcall.assert_not_called()


def test_bedrock_failure_falls_through():
    with patch.object(config, "PROVIDER_ORDER", ["bedrock", "gemini"]), \
         patch.object(config, "BEDROCK_ENABLED", True), \
         patch.object(config, "GEMINI_API_KEY", "g"), \
         patch("briefing.generator._bedrock_call", side_effect=RuntimeError("aws down")), \
         patch("briefing.generator._gemini_call", return_value="gemini reply"):
        assert _grok_call("sys", "user") == "gemini reply"


def test_no_provider_available_raises_clear_error():
    with patch.object(config, "PROVIDER_ORDER", ["bedrock", "gemini", "maxplus", "claude-cli"]), \
         patch.object(config, "BEDROCK_ENABLED", False), \
         patch.object(config, "GEMINI_API_KEY", ""), \
         patch.object(config, "MAXPLUS_API_KEY", ""), \
         patch.object(config, "CLAUDE_CLI_ENABLED", False):
        with pytest.raises(RuntimeError, match="No AI provider available"):
            _grok_call("sys", "user")


# ---- _call_with_retry: network-level errors (not HTTPError) also retry ----


def test_network_error_retries_then_succeeds():
    # URLError and TimeoutError (raised by urlopen on a connection failure,
    # not an HTTP response) must hit the same backoff-and-retry path as a
    # 429/503 HTTPError, not propagate on the first failure.
    with patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep") as sleep:
        urlopen.side_effect = [
            urllib.error.URLError("connection refused"),
            TimeoutError(),
            _ok_response(MAXPLUS_OK),
        ]
        result = _grok_call("sys", "user", max_attempts=4)

    assert result == "maxplus reply"
    assert urlopen.call_count == 3
    assert sleep.call_count == 2


def test_network_error_raises_last_error_after_exhausting_attempts():
    with patch.object(config, "PROVIDER_ORDER", ["maxplus"]), \
         patch.object(config, "MAXPLUS_API_KEY", "mp-key"), \
         patch("urllib.request.urlopen") as urlopen, \
         patch("time.sleep") as sleep:
        urlopen.side_effect = [
            urllib.error.URLError("down 1"),
            urllib.error.URLError("down 2"),
        ]
        with pytest.raises(urllib.error.URLError, match="down 2"):
            _grok_call("sys", "user", max_attempts=2)

    assert urlopen.call_count == 2
    assert sleep.call_count == 1


# ---- _gemini_call: thinking-model response with no text part -------------


def test_gemini_call_raises_on_thinking_only_response():
    # A thinking model's response can carry only thoughtSignature/functionCall
    # parts with no "text" key at all — must raise ValueError, not KeyError,
    # and must not retry (it's a deterministic shape problem, not flakiness).
    payload = {
        "candidates": [{
            "content": {
                "parts": [
                    {"thoughtSignature": "abc"},
                    {"functionCall": {"name": "x"}},
                ]
            }
        }]
    }
    with patch.object(config, "GEMINI_API_KEY", "g-key"), \
         patch("urllib.request.urlopen", return_value=_ok_response(payload)) as urlopen:
        with pytest.raises(ValueError, match="no text part"):
            _gemini_call("sys", "user", max_attempts=3)

    assert urlopen.call_count == 1  # not retried — the response was "successful" HTTP-wise


# ---- _bedrock_call: AnthropicBedrock client construction and dispatch ----


def _bedrock_text_response(text="bedrock reply"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _anthropic_error(cls, code, message="boom"):
    req = httpx.Request("POST", "https://bedrock.example.com")
    resp = httpx.Response(code, request=req, json={"error": {"message": message}})
    return cls(message, response=resp, body=None)


def test_bedrock_call_success_extracts_text():
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _bedrock_text_response("bedrock reply")

    with patch.object(config, "BEDROCK_REGION", "ap-southeast-1"), \
         patch.object(config, "BEDROCK_MODEL", "global.anthropic.claude-sonnet-5"), \
         patch.object(config, "BEDROCK_PROFILE", ""), \
         patch("anthropic.AnthropicBedrock", return_value=mock_client) as cls:
        result = _bedrock_call("sys", "user", max_attempts=3)

    assert result == "bedrock reply"
    cls.assert_called_once_with(
        aws_region="ap-southeast-1", aws_profile=None, timeout=120.0, max_retries=0
    )
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["model"] == "global.anthropic.claude-sonnet-5"
    assert kwargs["system"] == "sys"
    assert kwargs["thinking"] == {"type": "disabled"}
    assert kwargs["messages"] == [{"role": "user", "content": "user"}]


def test_bedrock_call_passes_named_profile_when_configured():
    # Without this, boto3-style env-var credential resolution can silently
    # pick up ambient AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY (e.g. set
    # system-wide for Claude Code's own CLAUDE_CODE_USE_BEDROCK) instead of
    # the identity this pipeline actually intends — BEDROCK_PROFILE pins it.
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _bedrock_text_response("bedrock reply")

    with patch.object(config, "BEDROCK_PROFILE", "ai-briefing"), \
         patch("anthropic.AnthropicBedrock", return_value=mock_client) as cls:
        _bedrock_call("sys", "user", max_attempts=3)

    _, kwargs = cls.call_args
    assert kwargs["aws_profile"] == "ai-briefing"


def test_bedrock_call_no_text_block_raises_value_error():
    thinking_block = MagicMock()
    thinking_block.type = "thinking"
    resp = MagicMock()
    resp.content = [thinking_block]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = resp

    with patch("anthropic.AnthropicBedrock", return_value=mock_client), \
         patch("briefing.generator.time.sleep") as sleep:
        with pytest.raises(ValueError, match="no text block"):
            _bedrock_call("sys", "user", max_attempts=2)

    assert mock_client.messages.create.call_count == 2  # a missing text block IS retried
    assert sleep.call_count == 1


@pytest.mark.parametrize("error_cls,code", [
    ("PermissionDeniedError", 403),
    ("AuthenticationError", 401),
    ("BadRequestError", 400),
    ("NotFoundError", 404),
])
def test_bedrock_call_fails_fast_on_deterministic_errors(error_cls, code):
    import anthropic as anthropic_sdk

    err = _anthropic_error(getattr(anthropic_sdk, error_cls), code)
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = err

    with patch("anthropic.AnthropicBedrock", return_value=mock_client), \
         patch("briefing.generator.time.sleep") as sleep:
        with pytest.raises(getattr(anthropic_sdk, error_cls)):
            _bedrock_call("sys", "user", max_attempts=3)

    assert mock_client.messages.create.call_count == 1  # no retry
    sleep.assert_not_called()


def test_bedrock_call_retries_rate_limit_then_succeeds():
    import anthropic as anthropic_sdk

    err = _anthropic_error(anthropic_sdk.RateLimitError, 429)
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [err, _bedrock_text_response("recovered")]

    with patch("anthropic.AnthropicBedrock", return_value=mock_client), \
         patch("briefing.generator.time.sleep") as sleep:
        result = _bedrock_call("sys", "user", max_attempts=3)

    assert result == "recovered"
    assert mock_client.messages.create.call_count == 2
    assert sleep.call_count == 1


def test_bedrock_call_retries_internal_server_error_then_raises_after_exhausting():
    import anthropic as anthropic_sdk

    err = _anthropic_error(anthropic_sdk.InternalServerError, 500)
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = err

    with patch("anthropic.AnthropicBedrock", return_value=mock_client), \
         patch("briefing.generator.time.sleep") as sleep:
        with pytest.raises(anthropic_sdk.InternalServerError):
            _bedrock_call("sys", "user", max_attempts=2)

    assert mock_client.messages.create.call_count == 2  # retried both attempts
    assert sleep.call_count == 1


# ---- fetch_recent_items: sqlite query against feed_items -------------------


def _connect_feeds_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "feeds.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE feed_items (
            title TEXT, content TEXT, url TEXT, source_type TEXT,
            published_at TEXT, fetched_at TEXT
        )"""
    )
    return conn


def test_fetch_recent_items_filters_by_since_hours_and_orders_newest_first(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Too old', 'oc', 'http://a', 'news', '2026-07-01', datetime('now', '-48 hours'))"
    )
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Older recent', 'rc1', 'http://b', 'news', '2026-07-30', datetime('now', '-10 hours'))"
    )
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Newer recent', 'rc2', 'http://c', 'news', '2026-07-30', datetime('now', '-1 hours'))"
    )
    conn.commit()

    items = fetch_recent_items(conn, since_hours=24)

    assert [i["title"] for i in items] == ["Newer recent", "Older recent"]


def test_fetch_recent_items_empty_when_no_rows_within_window(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Too old', 'oc', 'http://a', 'news', '2026-07-01', datetime('now', '-48 hours'))"
    )
    conn.commit()

    assert fetch_recent_items(conn, since_hours=24) == []


def test_fetch_recent_items_returns_plain_dicts(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Item', 'c', 'http://a', 'news', '2026-07-30', datetime('now', '-1 hours'))"
    )
    conn.commit()

    items = fetch_recent_items(conn, since_hours=24)

    assert items == [{
        "title": "Item", "content": "c", "url": "http://a", "source_type": "news",
        "published_at": "2026-07-30", "fetched_at": items[0]["fetched_at"],
    }]
    assert isinstance(items[0], dict) and not isinstance(items[0], sqlite3.Row)


# ---- call_gemini: round-robin section calls, concurrency, ordering --------


def _marker_item(i, source_type="news"):
    return {"title": f"Item-{i}", "url": f"http://x/{i}", "content": "", "source_type": source_type}


def test_call_gemini_invokes_grok_once_per_section():
    items = [_marker_item(i) for i in range(len(SECTION_PROMPTS))]
    with patch("briefing.generator._grok_call", return_value="section text") as grok:
        call_gemini(items, "2026-07-31", "")
    assert grok.call_count == len(SECTION_PROMPTS)


def test_call_gemini_joins_sections_with_divider_in_order_regardless_of_completion_order():
    import time

    items = [_marker_item(i) for i in range(len(SECTION_PROMPTS))]
    # Deliberately staggered delays so completion order != submission order —
    # the ThreadPoolExecutor must still assemble output in SECTION_PROMPTS order.
    delays = [0.05, 0.0, 0.03, 0.01, 0.04, 0.0]

    def fake_grok(system, user):
        for i in range(len(items)):
            if f"Item-{i}" in user:
                time.sleep(delays[i])
                return f"SECTION-{i}"
        raise AssertionError("no item marker found in prompt")

    with patch("briefing.generator._grok_call", side_effect=fake_grok):
        result = call_gemini(items, "2026-07-31", "")

    expected = "\n\n---\n\n".join(f"SECTION-{i}" for i in range(len(SECTION_PROMPTS)))
    assert result == expected


def test_call_gemini_sanitizes_items_and_research_findings_in_prompt():
    items = [{
        "title": "Bare url item", "url": "",
        "content": "See https://bare.example.com/x for an exploit walkthrough",
        "source_type": "news",
    }]
    captured = []

    def fake_grok(system, user):
        captured.append(user)
        return "OUT"

    with patch("briefing.generator._grok_call", side_effect=fake_grok):
        call_gemini(
            items, "2026-07-31", "",
            research_findings="### topic\nSee https://research.example.com/y for a bypass",
        )

    combined = "\n".join(captured)
    assert "https://bare.example.com/x" not in combined
    assert "https://research.example.com/y" not in combined
    assert "[security-related]" in combined


def test_call_gemini_appends_style_block_when_style_given():
    items = [_marker_item(0)]
    captured = []

    def fake_grok(system, user):
        captured.append(user)
        return "OUT"

    with patch("briefing.generator._grok_call", side_effect=fake_grok):
        call_gemini(items, "2026-07-31", "Always use short sentences.")

    assert any("STYLE RULES" in u and "Always use short sentences." in u for u in captured)


# ---- generate(): full pipeline (fetch -> budget -> Gemini -> HTML) -------


def _write_utf8(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _fake_briefing_markdown():
    return "# AI Briefing\n" + "\n\n---\n\n".join(
        f"## Section {i}\ncontent for section {i}" for i in range(1, 7)
    )


def test_generate_runs_full_pipeline_and_archives_files(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Item', 'c', 'http://a', 'news', '2026-07-30', datetime('now', '-1 hours'))"
    )
    conn.commit()

    archive_dir = tmp_path / "archives"
    fake_markdown = _fake_briefing_markdown()

    # generate() opens archive files with the platform-default encoding
    # (open(path, "w")) — on this Windows machine that's cp874, which can't
    # encode the emoji generate() writes into the receipt section. Forcing
    # utf-8 here matches how the file is actually read back for assertions;
    # generator.py itself is out of scope for this test-only change.
    with patch.object(config, "ARCHIVE_DIR", str(archive_dir)), \
         patch.object(config, "STYLE_PATH", str(tmp_path / "no_such_style.md")), \
         patch("briefing.generator.call_gemini", return_value=fake_markdown) as cg, \
         patch("briefing.generator.open", functools.partial(open, encoding="utf-8")):
        result = generate(conn)

    assert cg.call_count == 1
    assert set(result.keys()) == {
        "markdown", "part1_html", "part2_html", "date_str", "today", "archive_file",
    }
    assert result["markdown"] == fake_markdown
    assert result["archive_file"].startswith("briefing_") and result["archive_file"].endswith(".md")

    archived = {p.name for p in archive_dir.iterdir()}
    assert result["archive_file"] in archived
    assert any(n.endswith("_part1_news.md") for n in archived)
    assert any(n.endswith("_part2_technical.md") for n in archived)

    full_archive_text = (archive_dir / result["archive_file"]).read_text(encoding="utf-8")
    assert full_archive_text == fake_markdown


def test_generate_includes_research_receipt_in_part2_only(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.commit()

    archive_dir = tmp_path / "archives"
    fake_markdown = _fake_briefing_markdown()
    research_findings = "### My Research Topic\nSome findings text.\n"

    with patch.object(config, "ARCHIVE_DIR", str(archive_dir)), \
         patch.object(config, "STYLE_PATH", str(tmp_path / "no_such_style.md")), \
         patch("briefing.generator.call_gemini", return_value=fake_markdown), \
         patch("briefing.generator.open", functools.partial(open, encoding="utf-8")):
        result = generate(conn, research_findings=research_findings)

    # The receipt is appended in code (deterministic), landing in part 2
    # since it comes after the last (6th) section — split_two_parts cuts
    # everything from the 7th top-level "## " heading onward into part 2.
    assert "My Research Topic" not in result["part1_html"]
    assert "My Research Topic" in result["part2_html"]
    assert "Requested Research" in result["part2_html"]


def test_generate_passes_budgeted_items_and_research_findings_to_call_gemini(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Item', 'c', 'http://a', 'news', '2026-07-30', datetime('now', '-1 hours'))"
    )
    conn.commit()

    archive_dir = tmp_path / "archives"
    fake_markdown = _fake_briefing_markdown()
    research_findings = "### Topic\nfindings\n"

    with patch.object(config, "ARCHIVE_DIR", str(archive_dir)), \
         patch.object(config, "STYLE_PATH", str(tmp_path / "no_such_style.md")), \
         patch("briefing.generator.call_gemini", return_value=fake_markdown) as cg, \
         patch("briefing.generator.open", functools.partial(open, encoding="utf-8")):
        generate(conn, research_findings=research_findings)

    args, kwargs = cg.call_args
    called_items = args[0]
    assert any(i["title"] == "Item" for i in called_items)
    assert kwargs["research_findings"] == research_findings


# ---- build_social_post_source ----------------------------------------------


def test_build_social_post_source_includes_title_url_and_content():
    fetched = [{"title": "An Article", "url": "https://example.com/a", "content": "full article body"}]
    source = build_social_post_source(fetched)
    assert "An Article" in source
    assert "https://example.com/a" in source
    assert "full article body" in source


def test_build_social_post_source_joins_multiple_items():
    fetched = [
        {"title": "First", "url": "https://example.com/1", "content": "one"},
        {"title": "Second", "url": "https://example.com/2", "content": "two"},
    ]
    source = build_social_post_source(fetched)
    assert "First" in source and "Second" in source
    assert source.index("First") < source.index("Second")


def test_build_social_post_source_empty_list_gives_empty_string():
    assert build_social_post_source([]) == ""


# ---- generate_social_post ----------------------------------------------------


def test_generate_social_post_raises_on_empty_source_material():
    with pytest.raises(ValueError):
        generate_social_post("", "Thursday, July 30, 2026")


def test_generate_social_post_raises_on_whitespace_only_source_material():
    with pytest.raises(ValueError):
        generate_social_post("   \n  ", "Thursday, July 30, 2026")


def test_generate_social_post_delegates_to_grok_call_with_source_material():
    with patch("briefing.generator._grok_call", return_value="post text") as grok:
        result = generate_social_post("=== Article ===\nSource: https://x\n\nbody", "Thursday, July 30, 2026")

    assert result == "post text"
    grok.assert_called_once()
    system_arg, user_arg = grok.call_args.args
    assert "LinkedIn" in system_arg
    assert "body" in user_arg
    assert "Thursday, July 30, 2026" in user_arg


def test_generate_social_post_picks_one_of_three_style_angles():
    seen_labels = set()
    with patch("briefing.generator._grok_call") as grok:
        grok.side_effect = lambda system, user: user  # echo the prompt back
        for _ in range(30):
            user_prompt = generate_social_post("source material here", "date")
            for label, instructions in SOCIAL_POST_STYLE_ANGLES:
                if instructions in user_prompt:
                    seen_labels.add(label)
                    break

    # 30 random draws across 3 angles should hit more than just one, without
    # this test depending on any single specific angle being chosen
    assert len(seen_labels) > 1


def test_generate_social_post_sanitizes_source_material():
    with patch("briefing.generator._grok_call", return_value="post text") as grok:
        generate_social_post("visit https://bare-url.example for the 0-day exploit", "date")

    _, user_arg = grok.call_args.args
    assert "https://bare-url.example" not in user_arg
    assert "[security-related]" in user_arg


# ---- social_post_candidate_items --------------------------------------------


def _insert_ordered_items(conn, count):
    """Insert `count` items, each newer than the last (item0 = newest), so
    fetch_recent_items -> budget_items preserves this index order (same
    source_type/priority tier = stable sort keeps insertion order). Minutes,
    not hours — fetch_recent_items' default 24h window would silently drop
    anything past item23 if spaced an hour apart."""
    for i in range(count):
        conn.execute(
            "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
            "VALUES (?, 'c', 'http://x', 'news', '2026-07-30', datetime('now', ?))",
            (f"item{i}", f"-{i + 1} minutes"),
        )
    conn.commit()


def test_social_post_candidate_items_unattended_path_caps_per_section(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    _insert_ordered_items(conn, 30)  # 5 per section (30 / 6), capped to 3 by default

    candidates = social_post_candidate_items(conn)

    # section 0 = indices 0, 6, 12, 18, 24 -> capped to the first 3: 0, 6, 12
    section0_titles = [c["title"] for c in candidates if c["title"] in ("item0", "item6", "item12", "item18", "item24")]
    assert section0_titles == ["item0", "item6", "item12"]
    # every section contributed at most 3 items -> 6 sections * 3 = 18 total
    assert len(candidates) == 18


def test_social_post_candidate_items_respects_custom_max_per_section(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    _insert_ordered_items(conn, 30)

    candidates = social_post_candidate_items(conn, max_items_per_section=1)

    assert len(candidates) == len(SECTION_PROMPTS)  # exactly 1 per section


def test_social_post_candidate_items_manual_selection_is_uncapped(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    _insert_ordered_items(conn, 30)

    candidates = social_post_candidate_items(conn, section_indices=[0])

    # section 0's full slice (indices 0, 6, 12, 18, 24), NOT capped to 3 —
    # a human picked this section and is waiting on the result
    assert [c["title"] for c in candidates] == ["item0", "item6", "item12", "item18", "item24"]


def test_social_post_candidate_items_manual_selection_multiple_sections(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    _insert_ordered_items(conn, 12)  # 2 per section

    candidates = social_post_candidate_items(conn, section_indices=[0, 1])

    titles = {c["title"] for c in candidates}
    assert titles == {"item0", "item6", "item1", "item7"}


def test_social_post_candidate_items_empty_when_no_feed_items(tmp_path):
    conn = _connect_feeds_db(tmp_path)
    conn.commit()
    assert social_post_candidate_items(conn) == []
