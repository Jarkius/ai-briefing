"""Tests for mcp_client's cross-process lock plumbing.

The QA finding these guard against: mcp_lock/is_locked once called
fcntl.flock directly, making the msvcrt fallback (_try_lock/_unlock) dead
code — on Windows the first lock call raised NameError and collect/research
silently soft-failed every scheduled run. The lock paths must go through
the platform-dispatched helpers, which these tests patch.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from briefing import mcp_client


def test_mcp_lock_uses_platform_helper_and_fails_fast():
    # retry_seconds=0 (dashboard semantics): a held lock -> immediate LockHeldError
    with patch("briefing.mcp_client._try_lock", side_effect=BlockingIOError("held")):
        with pytest.raises(mcp_client.LockHeldError):
            with mcp_client.mcp_lock(retry_seconds=0):
                pass


def test_mcp_lock_acquires_and_releases_via_helpers():
    with patch("briefing.mcp_client._try_lock") as try_lock, \
         patch("briefing.mcp_client._unlock") as unlock:
        with mcp_client.mcp_lock():
            try_lock.assert_called_once()
            unlock.assert_not_called()
    unlock.assert_called_once()


def test_is_locked_true_when_helper_raises():
    with patch("briefing.mcp_client._try_lock", side_effect=BlockingIOError("held")):
        assert mcp_client.is_locked() is True


def test_is_locked_false_when_helper_succeeds():
    with patch("briefing.mcp_client._try_lock"), patch("briefing.mcp_client._unlock"):
        assert mcp_client.is_locked() is False


def test_lock_helpers_have_no_direct_fcntl_calls_in_lock_paths():
    # Regression guard for the dead-code finding: fcntl must only appear in
    # the platform-dispatch block at the top of the module, never inside
    # mcp_lock/is_locked bodies.
    import inspect

    for fn in (mcp_client.is_locked,):
        assert "fcntl" not in inspect.getsource(fn)
    # mcp_lock is a contextmanager-wrapped generator; inspect its wrapped func
    assert "fcntl" not in inspect.getsource(mcp_client.mcp_lock.__wrapped__)


# ---- mcp_lock retry-with-backoff (retry_seconds > 0) -----------------------


def test_mcp_lock_retries_then_acquires_when_lock_becomes_free():
    # deadline calc consumes 1 monotonic() call; each not-yet-expired
    # iteration consumes 2 more (the expiry check, then the sleep-duration
    # calc) before _try_lock finally succeeds on its 3rd attempt.
    monotonic_values = [0, 5, 5, 10, 10]
    entered = False
    with patch(
        "briefing.mcp_client._try_lock",
        side_effect=[BlockingIOError("held"), BlockingIOError("held"), None],
    ) as try_lock, \
         patch("briefing.mcp_client._unlock") as unlock, \
         patch("briefing.mcp_client.time.sleep") as mock_sleep, \
         patch("briefing.mcp_client.time.monotonic", side_effect=monotonic_values):
        with mcp_client.mcp_lock(retry_seconds=30, poll_interval=5):
            entered = True
    assert entered
    assert try_lock.call_count == 3
    assert mock_sleep.call_count == 2
    unlock.assert_called_once()


def test_mcp_lock_retries_then_raises_when_lock_stays_held():
    # Same accounting as above, but the deadline (10) is crossed on the 3rd
    # expiry check (12) instead of _try_lock ever succeeding.
    monotonic_values = [0, 3, 3, 8, 8, 12]
    with patch(
        "briefing.mcp_client._try_lock", side_effect=BlockingIOError("held")
    ) as try_lock, \
         patch("briefing.mcp_client.time.sleep") as mock_sleep, \
         patch("briefing.mcp_client.time.monotonic", side_effect=monotonic_values):
        with pytest.raises(mcp_client.LockHeldError):
            with mcp_client.mcp_lock(retry_seconds=10, poll_interval=5):
                pass
    assert try_lock.call_count == 3
    assert mock_sleep.call_count == 2


# ---- _server_params ---------------------------------------------------------


def test_server_params_filters_env_and_injects_feeds_db_path():
    fake_environ = {
        "PATH": "/usr/bin",
        "PLAYWRIGHT_BROWSERS_PATH": "/browsers",
        "GMAIL_APP_PASSWORD": "super-secret",
    }
    with patch("briefing.mcp_client.os.environ", fake_environ), \
         patch("briefing.mcp_client.config.FEEDS_DB_PATH", "/fake/data/feeds.db"):
        params = mcp_client._server_params()

    assert params.env["PATH"] == "/usr/bin"
    assert params.env["PLAYWRIGHT_BROWSERS_PATH"] == "/browsers"
    assert params.env["FEEDS_DB_PATH"] == "/fake/data/feeds.db"
    assert "GMAIL_APP_PASSWORD" not in params.env
    assert params.command == sys.executable
    assert params.args == ["-m", "google_search_mcp"]


# ---- McpSession -------------------------------------------------------------


def test_mcp_session_lifecycle_enters_and_exits_underlying_ctxs_in_order():
    events = []

    class _FakeStdioCtx:
        async def __aenter__(self):
            events.append("stdio_enter")
            return ("read-stream", "write-stream")

        async def __aexit__(self, exc_type, exc, tb):
            events.append("stdio_exit")

    class _FakeClientSession:
        def __init__(self, read, write):
            self.read = read
            self.write = write
            self.initialize = AsyncMock(side_effect=lambda: events.append("session_initialize"))

        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append("session_exit")

    with patch(
        "briefing.mcp_client.stdio_client", return_value=_FakeStdioCtx()
    ) as stdio_client_fn, \
         patch(
             "briefing.mcp_client.ClientSession", side_effect=_FakeClientSession
         ) as client_session_cls, \
         patch("briefing.mcp_client._server_params", return_value="fake-params"):

        async def run():
            async with mcp_client.McpSession() as session:
                assert isinstance(session, _FakeClientSession)
                return session

        session = asyncio.run(run())

    stdio_client_fn.assert_called_once_with("fake-params")
    client_session_cls.assert_called_once_with("read-stream", "write-stream")
    assert session.read == "read-stream"
    assert session.write == "write-stream"
    assert events == [
        "stdio_enter",
        "session_enter",
        "session_initialize",
        "session_exit",
        "stdio_exit",
    ]


def test_mcp_session_aexit_propagates_exception_and_still_tears_down():
    events = []

    class _FakeStdioCtx:
        async def __aenter__(self):
            return ("read-stream", "write-stream")

        async def __aexit__(self, exc_type, exc, tb):
            events.append(("stdio_exit", exc_type))

    class _FakeClientSession:
        def __init__(self, read, write):
            self.initialize = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append(("session_exit", exc_type))

    with patch("briefing.mcp_client.stdio_client", return_value=_FakeStdioCtx()), \
         patch("briefing.mcp_client.ClientSession", side_effect=_FakeClientSession), \
         patch("briefing.mcp_client._server_params", return_value="fake-params"):

        async def run():
            async with mcp_client.McpSession():
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            asyncio.run(run())

    assert events == [("session_exit", ValueError), ("stdio_exit", ValueError)]
