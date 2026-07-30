"""Tests for mcp_client's cross-process lock plumbing.

The QA finding these guard against: mcp_lock/is_locked once called
fcntl.flock directly, making the msvcrt fallback (_try_lock/_unlock) dead
code — on Windows the first lock call raised NameError and collect/research
silently soft-failed every scheduled run. The lock paths must go through
the platform-dispatched helpers, which these tests patch.
"""

from unittest.mock import patch

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
