"""Access to the vendored MCP server's feeds.db, plus our own `runs` table.

feed_items/subscriptions/feed_items_fts are owned by the vendored MCP server
(src/google_search_mcp/server.py in the fork) — we only read them, and guard
against upstream schema drift with a column check. `runs` is ours: insert-only,
one row per pipeline invocation, so cron and dashboard writers never update
the same row concurrently (see .omc/plans/2026-07-22-control-panel.md).
"""

import os
import sqlite3

from . import config

EXPECTED_FEED_ITEMS_COLUMNS = {
    "title", "content", "url", "source_type", "published_at", "fetched_at",
}


def connect() -> sqlite3.Connection:
    """Open feeds.db (creating data/ if needed), with busy-wait tolerance for
    a lingering WAL lock left by a killed MCP subprocess."""
    import os
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.FEEDS_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    _ensure_runs_table(conn)
    return conn


def assert_feed_items_schema(conn: sqlite3.Connection):
    """Raise a clear error if the vendored server's feed_items schema no
    longer has the columns we read. Call before querying feed_items —
    the table only exists once the MCP server has run check_feeds at least
    once, so a missing table is a distinct (and fine) case from a changed one."""
    rows = conn.execute("PRAGMA table_info(feed_items)").fetchall()
    if not rows:
        return  # table doesn't exist yet — no subscriptions checked yet, not a schema drift
    columns = {row["name"] for row in rows}
    missing = EXPECTED_FEED_ITEMS_COLUMNS - columns
    if missing:
        raise RuntimeError(
            f"vendored server schema changed — feed_items is missing columns {missing}. "
            "Review generator.py's queries against the current fork's server.py."
        )


def _ensure_runs_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            collect_status TEXT,
            research_status TEXT,
            generate_status TEXT,
            send_status TEXT,
            error_text TEXT
        );
    """)
    conn.commit()


def _retry_locked(fn, label: str, attempts: int = 3):
    """runs-table writes race the vendored MCP server's feed_items commits
    (different tables, same sqlite file — writers serialize DB-wide). A
    transient 'database is locked' on STATUS TRACKING must never kill the
    pipeline (hunt-data #2: run.py doesn't wrap update_run in the phase
    try/excepts). Retry briefly, then degrade to a log line."""
    import time

    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            if attempt == attempts - 1:
                print(f"[db] {label} skipped — database locked after {attempts} attempts", flush=True)
                return None
            time.sleep(1 + attempt)
    return None


def insert_run(conn: sqlite3.Connection, source: str, started_at: str) -> int:
    """Insert a new run row. `source` is 'cron' or 'dashboard'. Never update
    another writer's row — each call to insert_run owns exactly one row."""

    def _do():
        cur = conn.execute(
            "INSERT INTO runs (source, started_at) VALUES (?, ?)",
            (source, started_at),
        )
        conn.commit()
        return cur.lastrowid

    row_id = _retry_locked(_do, "insert_run")
    return row_id if row_id is not None else -1


def update_run(conn: sqlite3.Connection, run_id: int, **fields):
    """Update columns on a run row this process owns (by run_id)."""
    if not fields or run_id < 0:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)

    def _do():
        conn.execute(
            f"UPDATE runs SET {set_clause} WHERE id = ?",
            (*fields.values(), run_id),
        )
        conn.commit()

    _retry_locked(_do, "update_run")


def existing_subscription_names(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(source_type, name) pairs already present. Needed because the
    vendored server rewrites `identifier` for some source types (youtube:
    @handle -> resolved UC... channel_id; news presets -> the preset key
    unchanged; arxiv shortcuts -> the resolved category) after a network
    lookup — matching subscriptions.json against the *stored* identifier
    would never hit for those types, causing collector.py to re-attempt (and
    re-pay the network/resolution cost for) the same subscribe call every
    single run. `name` is stable across re-subscribe attempts, so it's the
    reliable reconciliation key."""
    rows = conn.execute("PRAGMA table_info(subscriptions)").fetchall()
    if not rows:
        return set()
    return {
        (row["source_type"], row["name"])
        for row in conn.execute("SELECT source_type, name FROM subscriptions")
    }


SEND_STATUS_PATH = os.path.join(config.DATA_DIR, "send_status.json")
SEND_LOCK_PATH = os.path.join(config.DATA_DIR, ".send.lock")

# Portable advisory lock (same platform split as mcp_client, duplicated here
# so db.py doesn't pull in the mcp SDK import chain). BLOCKING variant —
# holders keep it sub-second, so waiting beats failing.
try:
    import fcntl

    def _lock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock_fd(fd):
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


import contextlib


@contextlib.contextmanager
def send_lock():
    """Cross-process lock serializing (a) send_status.json read-modify-write
    and (b) the dedup-check-then-send sequence, between cron and the panel
    on the same machine. Found by hunt-data: two concurrent
    record_send_status calls silently lost one writer's key, and two
    same-subject sends inside the mailbox-visibility window could both pass
    the already-sent check."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd = os.open(SEND_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        _lock_fd(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            _unlock_fd(fd)
        os.close(fd)


def record_send_status(archive_file: str, result: dict) -> None:
    """Persist the per-archive send outcome (data/send_status.json,
    per-machine like feeds.db). Called after every send attempt so the
    panel's Archive tab can badge which issues actually reached the inbox.
    result is send_two_part_briefing's dict: {'part1': 'sent'|'already_sent'
    |'error: …', 'part2': …}."""
    import json
    from datetime import datetime

    statuses = set(result.values())
    delivered = {"sent", "already_sent"}
    if statuses <= delivered:
        status = "sent"
    elif statuses & delivered:
        # Some parts reached the inbox, some failed — 'error' here would
        # falsely imply nothing delivered (found by hunt-5am: the old
        # ordering made this branch dead code).
        status = "partial"
    else:
        status = "error"
    # Under the lock: load→mutate→replace is a lost-update race when cron's
    # post-send record collides with a panel archive-send record.
    with send_lock():
        log = load_send_status()
        log[archive_file] = {
            "status": status,
            "detail": result,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = SEND_STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(log, f, indent=2)
        os.replace(tmp, SEND_STATUS_PATH)


def load_send_status() -> dict:
    import json

    if not os.path.exists(SEND_STATUS_PATH):
        return {}
    try:
        with open(SEND_STATUS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def latest_phase_status(conn: sqlite3.Connection, phase_column: str) -> sqlite3.Row | None:
    """Latest non-null value for one phase column, independent of which run
    row holds it — a dashboard-only send after a cron collect still reports
    both correctly without either overwriting the other."""
    row = conn.execute(
        f"SELECT * FROM runs WHERE {phase_column} IS NOT NULL "
        f"ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row
