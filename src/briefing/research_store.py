"""Durable store for panel-submitted research tasks.

Fixes a real data-loss bug: research findings previously lived only in
src/panel/state.py's LAST_RESEARCH_FINDINGS (an in-process global), while
research_requests.md's checkbox was flipped to [x] the moment
researcher.run_pending_async() *returned* — regardless of whether the
findings ever reached a generated edition, or survived a server restart.
The checkbox meant "the research function returned," not "these findings
are durable" or "these findings were included in an edition." A user's
pasted research (no URL, multi-paragraph) was found checked off with zero
trace in any archive — permanently lost with no indication anything had
gone wrong.

This is deliberately a narrow slice, not a copy of the future
briefing.workflow_store.Store: same conceptual shape (stable task id,
state machine, persisted result text, consumption record) so a later
migration to the full workflow system is additive, not a rewrite — but
scoped to only what the panel's research flow needs today. run.py,
generator.py, and sender.py are untouched; the scheduled 5am pipeline
does not use this module.

States: queued -> ready | failed, ready -> consumed. There is no
"running" state (unlike workflow_store's research_tasks) because this
store's writer is the same synchronous call that produces the result —
see insert_and_mark_ready/insert_and_mark_failed.
"""

import sqlite3
from datetime import datetime

from . import config


def connect() -> sqlite3.Connection:
    return connect_at(config.RESEARCH_TASKS_DB_PATH)


def connect_at(db_path: str) -> sqlite3.Connection:
    """Path-parameterized variant of connect() — lets tests point at an
    isolated tmp_path file without needing config.DATA_DIR to exist."""
    import os

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            input_text TEXT NOT NULL,
            state TEXT NOT NULL,
            result_text TEXT,
            error_text TEXT,
            archive_file TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            consumed_at TEXT
        );
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def insert_queued(conn: sqlite3.Connection, input_text: str) -> int:
    cur = conn.execute(
        "INSERT INTO research_tasks (input_text, state, created_at) VALUES (?, 'queued', ?)",
        (input_text, _now()),
    )
    conn.commit()
    return cur.lastrowid


def mark_ready(conn: sqlite3.Connection, task_id: int, result_text: str) -> None:
    conn.execute(
        "UPDATE research_tasks SET state = 'ready', result_text = ?, finished_at = ? WHERE id = ?",
        (result_text, _now(), task_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, task_id: int, error_text: str) -> None:
    conn.execute(
        "UPDATE research_tasks SET state = 'failed', error_text = ?, finished_at = ? WHERE id = ?",
        (error_text, _now(), task_id),
    )
    conn.commit()


def list_ready(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM research_tasks WHERE state = 'ready' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def list_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Newest-first, for the panel's Requests list — includes every state
    so the editor can see queued/ready/failed/consumed, not just pending."""
    rows = conn.execute("SELECT * FROM research_tasks ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_task(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def search_tasks(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Newest-first, filtered to tasks whose input or findings text
    contains `query` (case-insensitive substring — no FTS index for a
    dataset this small; a full-text index would be premature complexity
    for a personal research log)."""
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM research_tasks "
        "WHERE input_text LIKE ? COLLATE NOCASE OR result_text LIKE ? COLLATE NOCASE "
        "ORDER BY id DESC",
        (like, like),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_consumed(conn: sqlite3.Connection, task_ids: list[int], archive_file: str) -> None:
    """Records which archive a batch of ready tasks' findings landed in.
    Called only AFTER generate() has successfully produced that archive —
    if the process dies between generation and this call, the tasks stay
    'ready' (visible, re-includable), never silently marked done without
    a real destination. A duplicate inclusion on retry is an acceptable
    cost; silent loss or a false "included" claim is not."""
    if not task_ids:
        return
    now = _now()
    conn.executemany(
        "UPDATE research_tasks SET state = 'consumed', archive_file = ?, consumed_at = ? "
        "WHERE id = ? AND state = 'ready'",
        [(archive_file, now, tid) for tid in task_ids],
    )
    conn.commit()
