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
