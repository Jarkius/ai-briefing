"""Tests for db.py: feed_items schema guard and the `runs` table helpers.

Fixture SQLite lives in tmp_path — no touching the real data/feeds.db.
"""

import sqlite3

import pytest

from briefing.db import (
    assert_feed_items_schema,
    existing_subscription_names,
    insert_run,
    latest_phase_status,
    update_run,
)


def _connect(tmp_path):
    conn = sqlite3.connect(tmp_path / "feeds.db")
    conn.row_factory = sqlite3.Row
    return conn


# ---- connect ----------------------------------------------------------------


def test_connect_creates_data_dir_and_configures_connection(tmp_path):
    from unittest.mock import patch

    from briefing import db as bdb

    data_dir = tmp_path / "newdata"
    db_path = data_dir / "feeds.db"
    with patch.object(bdb.config, "DATA_DIR", str(data_dir)), \
         patch.object(bdb.config, "FEEDS_DB_PATH", str(db_path)):
        assert not data_dir.exists()
        conn = bdb.connect()
    try:
        assert data_dir.exists()  # os.makedirs actually ran

        row = conn.execute("SELECT 1 AS one").fetchone()
        assert row["one"] == 1  # row_factory is sqlite3.Row, not a bare tuple

        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy_timeout == 15000
    finally:
        conn.close()


# ---- assert_feed_items_schema ---------------------------------------------


def test_schema_guard_passes_with_all_expected_columns(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        """CREATE TABLE feed_items (
            title TEXT, content TEXT, url TEXT, source_type TEXT,
            published_at TEXT, fetched_at TEXT
        )"""
    )
    assert_feed_items_schema(conn)  # must not raise


def test_schema_guard_passes_with_extra_columns(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        """CREATE TABLE feed_items (
            id INTEGER PRIMARY KEY, title TEXT, content TEXT, url TEXT,
            source_type TEXT, published_at TEXT, fetched_at TEXT, extra_col TEXT
        )"""
    )
    assert_feed_items_schema(conn)  # extra columns are fine


def test_schema_guard_raises_on_missing_column(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        """CREATE TABLE feed_items (
            title TEXT, content TEXT, url TEXT, source_type TEXT, published_at TEXT
            -- fetched_at deliberately missing
        )"""
    )
    with pytest.raises(RuntimeError, match="fetched_at"):
        assert_feed_items_schema(conn)


def test_schema_guard_noop_when_table_does_not_exist(tmp_path):
    conn = _connect(tmp_path)
    assert_feed_items_schema(conn)  # must not raise — table just not created yet


# ---- runs table helpers -----------------------------------------------------


def _ensure_runs_table(conn):
    conn.executescript(
        """
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
        """
    )
    conn.commit()


def test_insert_run_and_update_run(tmp_path):
    conn = _connect(tmp_path)
    _ensure_runs_table(conn)

    run_id = insert_run(conn, source="cron", started_at="2026-07-23T08:00:00")
    update_run(conn, run_id, send_status="sent", finished_at="2026-07-23T08:05:00")

    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["source"] == "cron"
    assert row["send_status"] == "sent"
    assert row["finished_at"] == "2026-07-23T08:05:00"


def test_latest_phase_status_returns_most_recent_non_null(tmp_path):
    conn = _connect(tmp_path)
    _ensure_runs_table(conn)

    older = insert_run(conn, source="cron", started_at="2026-07-22T08:00:00")
    update_run(conn, older, send_status="sent")
    newer = insert_run(conn, source="dashboard", started_at="2026-07-23T08:00:00")
    update_run(conn, newer, send_status="already_sent")

    row = latest_phase_status(conn, "send_status")
    assert row["id"] == newer
    assert row["send_status"] == "already_sent"


def test_latest_phase_status_skips_rows_with_null_phase(tmp_path):
    conn = _connect(tmp_path)
    _ensure_runs_table(conn)

    with_status = insert_run(conn, source="cron", started_at="2026-07-22T08:00:00")
    update_run(conn, with_status, collect_status="ok")
    insert_run(conn, source="dashboard", started_at="2026-07-23T08:00:00")  # send_status stays NULL

    row = latest_phase_status(conn, "collect_status")
    assert row["id"] == with_status


def test_latest_phase_status_none_when_no_rows(tmp_path):
    conn = _connect(tmp_path)
    _ensure_runs_table(conn)
    assert latest_phase_status(conn, "send_status") is None


# ---- existing_subscription_names -------------------------------------------


def test_existing_subscription_names_noop_when_table_missing(tmp_path):
    conn = _connect(tmp_path)
    assert existing_subscription_names(conn) == set()


def test_existing_subscription_names_returns_pairs(tmp_path):
    conn = _connect(tmp_path)
    conn.execute("CREATE TABLE subscriptions (source_type TEXT, name TEXT)")
    conn.execute("INSERT INTO subscriptions VALUES ('youtube', 'some-channel')")
    conn.execute("INSERT INTO subscriptions VALUES ('news', 'some-preset')")
    conn.commit()

    assert existing_subscription_names(conn) == {
        ("youtube", "some-channel"),
        ("news", "some-preset"),
    }


def test_record_send_status_partial_when_mixed(tmp_path):
    from unittest.mock import patch

    from briefing import db as bdb

    with patch.object(bdb, "SEND_STATUS_PATH", str(tmp_path / "s.json")), \
         patch.object(bdb.config, "DATA_DIR", str(tmp_path)):
        bdb.record_send_status("a.md", {"part1": "sent", "part2": "error: blip"})
        bdb.record_send_status("b.md", {"part1": "error: x", "part2": "error: y"})
        log = bdb.load_send_status()
    assert log["a.md"]["status"] == "partial"  # half delivered != error
    assert log["b.md"]["status"] == "error"


def test_send_lock_serializes_record_send_status(tmp_path):
    # hunt-data #1: concurrent read-modify-write lost one writer's key.
    import threading
    from unittest.mock import patch

    from briefing import db as bdb

    with patch.object(bdb, "SEND_STATUS_PATH", str(tmp_path / "s.json")), \
         patch.object(bdb, "SEND_LOCK_PATH", str(tmp_path / ".lock")), \
         patch.object(bdb.config, "DATA_DIR", str(tmp_path)):
        threads = [
            threading.Thread(target=bdb.record_send_status, args=(f"briefing_{i}.md", {"part1": "sent", "part2": "sent"}))
            for i in range(8)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        log = bdb.load_send_status()
    assert len(log) == 8  # no lost updates


def test_insert_run_degrades_on_persistent_lock(tmp_path):
    from unittest.mock import MagicMock, patch

    from briefing import db as bdb

    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError("database is locked")
    with patch("time.sleep"):
        row_id = bdb.insert_run(conn, source="cron", started_at="2026-07-30T05:00:00")
    assert row_id == -1  # degraded, not raised


def test_update_run_noop_for_degraded_run_id():
    from unittest.mock import MagicMock

    from briefing import db as bdb

    conn = MagicMock()
    bdb.update_run(conn, -1, send_status="ok")
    conn.execute.assert_not_called()


def test_insert_run_reraises_non_lock_operational_error(tmp_path):
    from unittest.mock import MagicMock, patch

    from briefing import db as bdb

    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError("no such table: runs")
    with patch("time.sleep"):
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            bdb.insert_run(conn, source="cron", started_at="2026-07-30T05:00:00")


# ---- load_send_status --------------------------------------------------------


def test_load_send_status_returns_empty_dict_on_corrupt_json(tmp_path):
    from unittest.mock import patch

    from briefing import db as bdb

    status_path = tmp_path / "s.json"
    status_path.write_text("{not valid json")
    with patch.object(bdb, "SEND_STATUS_PATH", str(status_path)):
        assert bdb.load_send_status() == {}
