"""Tests for collector.py: subscription reconciliation and the collect pipeline.

No real MCP server or network — sessions are AsyncMock/fake context managers,
locks and schema checks are patched out. Reconcile tests use a real tmp_path
sqlite conn for existing_subscription_names, matching test_db.py's pattern.
"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from briefing import collector, mcp_client
from briefing.collector import _check_feeds, _reconcile, run, run_async


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
