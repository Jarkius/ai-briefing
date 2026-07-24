"""Shared MCP stdio session wrapper for the vendored noapi-google-search-mcp
server, plus the cross-process file lock that keeps two independent sessions
(e.g. a cron `briefing.py` run and the dashboard's long-lived session) from
touching feeds.db at the same time.

See .omc/plans/2026-07-22-control-panel.md "Cross-process lock" for the
rationale — the collector plan's single-session invariant only covers
sequencing *within* one process; this lock is what covers *across* processes.
"""

import contextlib
import os
import sys
import time

# fcntl is Unix-only; on Windows fall back to msvcrt so importing this module
# (run.py → collector.py → here) doesn't kill the Windows scheduled run the
# repo also ships. Both give an exclusive, kernel-released advisory lock.
try:
    import fcntl

    def _try_lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if held

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _try_lock(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError(str(e)) from e

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import config

def _server_params() -> StdioServerParameters:
    """Built lazily (not at import time) so config.DATA_DIR reflects the
    current config module state, and so the vendored server's FEEDS_DB_PATH
    env var support (server.py ~line 5386) points at our repo's data/
    directory instead of its own ~/.cache default."""
    # Minimal env, not a full os.environ copy — the vendored third-party
    # server has no business receiving GMAIL_APP_PASSWORD or the API keys.
    # It needs PATH/HOME (Playwright + model caches are HOME-relative,
    # critical under launchd), locale, and our DB path override.
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ")
        or k.startswith(("PLAYWRIGHT_", "HF_", "XDG_"))
    }
    env["FEEDS_DB_PATH"] = config.FEEDS_DB_PATH
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "google_search_mcp"],
        env=env,
    )


class LockHeldError(RuntimeError):
    """Raised when the MCP lock could not be acquired."""


@contextlib.contextmanager
def mcp_lock(retry_seconds: float = 0, poll_interval: float = 5):
    """Acquire data/.mcp.lock for the duration of the block.

    retry_seconds=0 (default): fail immediately with LockHeldError if the
    lock is already held — used by the dashboard's live research jobs, which
    should never block a user-facing request.

    retry_seconds>0: retry with backoff up to that many seconds before
    giving up — used by the cron collector, which should tolerate a
    dashboard research job briefly holding the lock rather than failing the
    whole daily run over it.
    """
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd = os.open(config.MCP_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + retry_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockHeldError(
                        "data/.mcp.lock is held by another process (dashboard or cron run)."
                    )
                time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def is_locked() -> bool:
    """Non-blocking check of whether the lock is currently held, for the
    dashboard's /status indicator. Does not itself acquire or hold the lock."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd = os.open(config.MCP_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


class McpSession:
    """Thin async context manager around an MCP stdio ClientSession.

    Usage:
        async with McpSession() as session:
            await session.call_tool("google_search", {...})

    One instance = one subprocess + one session. The collector plan's
    "one session per run" design means callers open exactly one of these per
    pipeline invocation (or per dashboard server lifetime), not one per tool
    call.
    """

    def __init__(self):
        self._stdio_ctx = None
        self._session_ctx = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> ClientSession:
        self._stdio_ctx = stdio_client(_server_params())
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self.session = await self._session_ctx.__aenter__()
        await self.session.initialize()
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        if self._session_ctx is not None:
            await self._session_ctx.__aexit__(exc_type, exc, tb)
        if self._stdio_ctx is not None:
            await self._stdio_ctx.__aexit__(exc_type, exc, tb)


def tool_text(result) -> str:
    """Extract concatenated text content from an MCP call_tool result."""
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )
