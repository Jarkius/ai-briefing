"""Tests for collector.py: subscription reconciliation and the collect pipeline.

No real MCP server or network — sessions are AsyncMock/fake context managers,
locks and schema checks are patched out. Reconcile tests use a real tmp_path
sqlite conn for existing_subscription_names, matching test_db.py's pattern.
"""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from briefing import collector, config, mcp_client
from briefing.collector import (
    _check_feeds,
    _disable_source,
    _export_source_archive,
    _parse_check_feeds_result,
    _reconcile,
    _update_failure_streaks,
    run,
    run_async,
)


def _connect(tmp_path):
    conn = sqlite3.connect(tmp_path / "feeds.db")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_existing(conn, pairs):
    conn.execute("CREATE TABLE subscriptions (source_type TEXT, name TEXT)")
    for source_type, name in pairs:
        conn.execute("INSERT INTO subscriptions VALUES (?, ?)", (source_type, name))
    conn.commit()


def _text_result(text: str):
    """Fake an MCP call_tool() result shaped like mcp_client.tool_text expects."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


# ---- _reconcile -------------------------------------------------------------


def test_reconcile_skips_invalid_source_type(tmp_path):
    conn = _connect(tmp_path)
    session = AsyncMock()
    subs = [{"source_type": "bogus", "identifier": "x", "name": "foo"}]

    with patch.object(collector.config, "load_subscriptions", return_value=subs):
        added = asyncio.run(_reconcile(session, conn))

    assert added == 0
    session.call_tool.assert_not_awaited()


def test_reconcile_skips_missing_name(tmp_path):
    conn = _connect(tmp_path)
    session = AsyncMock()
    subs = [{"source_type": "news", "identifier": "y"}]  # no "name" field

    with patch.object(collector.config, "load_subscriptions", return_value=subs):
        added = asyncio.run(_reconcile(session, conn))

    assert added == 0
    session.call_tool.assert_not_awaited()


def test_reconcile_skips_disabled_source(tmp_path):
    conn = _connect(tmp_path)
    session = AsyncMock()
    subs = [{"source_type": "news", "identifier": "x", "name": "Flaky Feed", "enabled": False}]

    with patch.object(collector.config, "load_subscriptions", return_value=subs):
        added = asyncio.run(_reconcile(session, conn))

    assert added == 0
    session.call_tool.assert_not_awaited()


def test_reconcile_skips_already_subscribed(tmp_path):
    conn = _connect(tmp_path)
    _seed_existing(conn, [("youtube", "somechannel")])
    session = AsyncMock()
    subs = [{"source_type": "youtube", "identifier": "@handle", "name": "somechannel"}]

    with patch.object(collector.config, "load_subscriptions", return_value=subs):
        added = asyncio.run(_reconcile(session, conn))

    assert added == 0
    session.call_tool.assert_not_awaited()


def test_reconcile_subscribes_new_source_and_counts_it(tmp_path):
    conn = _connect(tmp_path)
    session = AsyncMock()
    session.call_tool.return_value = _text_result("subscribed ok")
    subs = [{"source_type": "youtube", "identifier": "@handle", "name": "newchannel"}]

    with patch.object(collector.config, "load_subscriptions", return_value=subs):
        added = asyncio.run(_reconcile(session, conn))

    assert added == 1
    session.call_tool.assert_awaited_once_with("subscribe", {
        "source_type": "youtube",
        "identifier": "@handle",
        "name": "newchannel",
    })


# ---- _check_feeds -----------------------------------------------------------


def test_check_feeds_calls_tool_and_returns_parsed_text():
    session = AsyncMock()
    session.call_tool.return_value = _text_result("feed check output")

    result = asyncio.run(_check_feeds(session))

    session.call_tool.assert_awaited_once_with("check_feeds", {})
    assert result == "feed check output"


# ---- _parse_check_feeds_result -----------------------------------------------

# Pinned against real briefing.log excerpts (the DNS-outage run and the
# Reddit 429 streak seen in production) so an upstream format change is
# caught immediately rather than silently mis-parsed.

_CLEAN_RUN = """Feed Check Complete

  Hacker News (top): 5 new items
  TechCrunch: 0 new items
  LangChain Blog: 1 new items

Total: 6 new items across 3 sources"""

_SINGLE_SOURCE_FAILURE = """Feed Check Complete

  Hacker News (top): 3 new items
  TechCrunch: 0 new items
  Reddit r/MachineLearning: ERROR — HTTP Error 429: Too Many Requests

Total: 3 new items across 3 sources"""

_NETWORK_WIDE_OUTAGE = """Feed Check Complete

  Hacker News (top): ERROR — <urlopen error [Errno 8] nodename nor servname provided, or not known>
  TechCrunch: ERROR — <urlopen error [Errno 8] nodename nor servname provided, or not known>
  PyTorch Blog: ERROR — <urlopen error [Errno 8] nodename nor servname provided, or not known>

Total: 0 new items across 3 sources"""


def test_parse_check_feeds_result_clean_run():
    assert _parse_check_feeds_result(_CLEAN_RUN) == {
        "Hacker News (top)": True,
        "TechCrunch": True,
        "LangChain Blog": True,
    }


def test_parse_check_feeds_result_single_source_failure():
    assert _parse_check_feeds_result(_SINGLE_SOURCE_FAILURE) == {
        "Hacker News (top)": True,
        "TechCrunch": True,
        "Reddit r/MachineLearning": False,
    }


def test_parse_check_feeds_result_ignores_total_summary_line():
    result = _parse_check_feeds_result(_NETWORK_WIDE_OUTAGE)
    assert "Total" not in result
    assert result == {
        "Hacker News (top)": False,
        "TechCrunch": False,
        "PyTorch Blog": False,
    }


# ---- _update_failure_streaks --------------------------------------------------


def test_update_failure_streaks_network_wide_outage_touches_no_counters():
    """Every source failed in the same run — that's the DNS blip pattern from
    real logs, not a per-source problem. No counters change, nothing disables."""
    subs = [
        {"source_type": "news", "identifier": "a", "name": "Hacker News (top)", "consecutive_failures": 2},
        {"source_type": "news", "identifier": "b", "name": "TechCrunch", "consecutive_failures": 0},
    ]
    with patch.object(config, "load_subscriptions", return_value=subs), \
         patch.object(config, "save_subscriptions") as mock_save:
        newly_disabled = _update_failure_streaks({
            "Hacker News (top)": False, "TechCrunch": False,
        })

    assert newly_disabled == []
    mock_save.assert_not_called()


def test_update_failure_streaks_single_source_increments_only_that_one():
    subs = [
        {"source_type": "news", "identifier": "a", "name": "Hacker News (top)", "consecutive_failures": 0},
        {"source_type": "news", "identifier": "b", "name": "Reddit r/MachineLearning", "consecutive_failures": 2},
    ]
    with patch.object(config, "load_subscriptions", return_value=subs), \
         patch.object(config, "save_subscriptions") as mock_save:
        newly_disabled = _update_failure_streaks({
            "Hacker News (top)": True, "Reddit r/MachineLearning": False,
        })

    assert newly_disabled == []
    saved = mock_save.call_args[0][0]
    assert next(s for s in saved if s["name"] == "Hacker News (top)")["consecutive_failures"] == 0
    assert next(s for s in saved if s["name"] == "Reddit r/MachineLearning")["consecutive_failures"] == 3


def test_update_failure_streaks_crossing_disable_threshold_returns_name():
    subs = [
        {"source_type": "news", "identifier": "a", "name": "Hacker News (top)", "consecutive_failures": 0},
        {
            "source_type": "news", "identifier": "b", "name": "Reddit r/MachineLearning",
            "consecutive_failures": config.FAILURE_DISABLE_THRESHOLD - 1,
        },
    ]
    with patch.object(config, "load_subscriptions", return_value=subs), \
         patch.object(config, "save_subscriptions"):
        # Hacker News succeeding is what makes this "one flaky source", not
        # a network-wide outage — see the guard this exercises.
        newly_disabled = _update_failure_streaks({
            "Hacker News (top)": True, "Reddit r/MachineLearning": False,
        })

    assert newly_disabled == ["Reddit r/MachineLearning"]


def test_update_failure_streaks_success_resets_counter():
    subs = [{"source_type": "news", "identifier": "a", "name": "TechCrunch", "consecutive_failures": 4}]
    with patch.object(config, "load_subscriptions", return_value=subs), \
         patch.object(config, "save_subscriptions") as mock_save:
        _update_failure_streaks({"TechCrunch": True})

    saved = mock_save.call_args[0][0]
    assert saved[0]["consecutive_failures"] == 0


def test_update_failure_streaks_empty_results_is_noop():
    with patch.object(config, "save_subscriptions") as mock_save:
        assert _update_failure_streaks({}) == []
    mock_save.assert_not_called()


# ---- _export_source_archive / _disable_source ---------------------------------


def _seed_feed_items(conn, name: str, items: list[dict]):
    conn.execute(
        "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, source_type TEXT, name TEXT)"
    )
    conn.execute("INSERT INTO subscriptions (source_type, name) VALUES ('news', ?)", (name,))
    sub_id = conn.execute("SELECT id FROM subscriptions WHERE name = ?", (name,)).fetchone()[0]
    conn.execute(
        "CREATE TABLE feed_items (id INTEGER PRIMARY KEY, subscription_id INTEGER, title TEXT, url TEXT)"
    )
    for item in items:
        conn.execute(
            "INSERT INTO feed_items (subscription_id, title, url) VALUES (?, ?, ?)",
            (sub_id, item["title"], item["url"]),
        )
    conn.commit()


def test_export_source_archive_writes_valid_json_with_stored_items(tmp_path):
    """Nothing is Deleted (workspace Prime Directive #1) — a source's stored
    items must round-trip to disk as valid JSON before unsubscribe cascades
    them out of feeds.db."""
    conn = _connect(tmp_path)
    _seed_feed_items(conn, "PyTorch Blog", [
        {"title": "Release notes", "url": "https://pytorch.org/a"},
        {"title": "Another post", "url": "https://pytorch.org/b"},
    ])

    with patch.object(collector.config, "REPO_ROOT", str(tmp_path)):
        path = _export_source_archive(conn, "PyTorch Blog")

    assert path.startswith(str(tmp_path / "archives" / "sources"))
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["source_name"] == "PyTorch Blog"
    assert len(data["items"]) == 2
    assert {i["title"] for i in data["items"]} == {"Release notes", "Another post"}


def test_disable_source_archives_then_unsubscribes_then_disables(tmp_path):
    conn = _connect(tmp_path)
    _seed_feed_items(conn, "Reddit r/MachineLearning", [{"title": "x", "url": "https://reddit.com/x"}])
    session = AsyncMock()
    session.call_tool.return_value = _text_result("unsubscribed ok")
    subs = [{
        "source_type": "reddit", "identifier": "MachineLearning",
        "name": "Reddit r/MachineLearning", "consecutive_failures": 5,
    }]

    with patch.object(collector.config, "REPO_ROOT", str(tmp_path)), \
         patch.object(collector.config, "load_subscriptions", return_value=subs), \
         patch.object(collector.config, "save_subscriptions") as mock_save:
        asyncio.run(_disable_source(session, conn, "Reddit r/MachineLearning"))

    session.call_tool.assert_awaited_once_with("unsubscribe", {
        "source_type": "reddit", "identifier": "MachineLearning",
    })
    saved = mock_save.call_args[0][0]
    assert saved[0]["enabled"] is False


def test_disable_source_skips_unknown_name(tmp_path):
    conn = _connect(tmp_path)
    session = AsyncMock()

    with patch.object(collector.config, "load_subscriptions", return_value=[]):
        asyncio.run(_disable_source(session, conn, "Nonexistent Source"))

    session.call_tool.assert_not_awaited()


# ---- run_async ---------------------------------------------------------------


def _fake_mcp_session_cls(session):
    class _FakeMcpSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return None

    return _FakeMcpSession


def test_run_async_happy_path_returns_ok():
    conn = MagicMock()
    session = MagicMock()

    with patch("briefing.collector.mcp_client.mcp_lock") as mock_lock, \
         patch("briefing.collector.mcp_client.McpSession", _fake_mcp_session_cls(session)), \
         patch("briefing.collector._reconcile", new=AsyncMock(return_value=2)) as mock_reconcile, \
         patch("briefing.collector._check_feeds", new=AsyncMock(return_value="feed text")) as mock_check_feeds, \
         patch("briefing.collector.db.assert_feed_items_schema") as mock_assert_schema:
        result = asyncio.run(run_async(1, conn))

    assert result == "ok"
    mock_lock.assert_called_once_with(retry_seconds=120)
    mock_reconcile.assert_awaited_once_with(session, conn)
    mock_check_feeds.assert_awaited_once_with(session)
    mock_assert_schema.assert_called_once_with(conn)


def test_run_async_soft_fail_when_lock_held():
    conn = MagicMock()

    with patch("briefing.collector.mcp_client.mcp_lock", side_effect=mcp_client.LockHeldError("held")), \
         patch("briefing.collector.db.assert_feed_items_schema") as mock_assert_schema:
        result = asyncio.run(run_async(1, conn))

    assert result == "soft_fail"
    mock_assert_schema.assert_not_called()


# ---- run (sync wrapper) -------------------------------------------------------


def test_run_delegates_to_run_async_and_returns_its_result():
    conn = MagicMock()

    with patch("briefing.collector.run_async", new=AsyncMock(return_value="ok")) as mock_run_async:
        result = run(42, conn)

    assert result == "ok"
    mock_run_async.assert_awaited_once_with(42, conn)
