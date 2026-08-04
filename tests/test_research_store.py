"""Tests for research_store.py: durable panel research-task tracking.

Fixture SQLite lives in tmp_path — no touching the real
data/research_tasks.db.
"""

from unittest.mock import patch

from briefing import research_store


def _connect(tmp_path):
    with patch.object(research_store.config, "DATA_DIR", str(tmp_path)), \
         patch.object(research_store.config, "RESEARCH_TASKS_DB_PATH", str(tmp_path / "research_tasks.db")):
        return research_store.connect()


def test_connect_creates_schema(tmp_path):
    conn = _connect(tmp_path)
    rows = conn.execute("PRAGMA table_info(research_tasks)").fetchall()
    columns = {r["name"] for r in rows}
    assert {"id", "input_text", "state", "result_text", "archive_file"} <= columns


def test_insert_queued_then_mark_ready_round_trips(tmp_path):
    conn = _connect(tmp_path)
    task_id = research_store.insert_queued(conn, "some topic")

    tasks = research_store.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0]["state"] == "queued"
    assert tasks[0]["input_text"] == "some topic"

    research_store.mark_ready(conn, task_id, "the findings")
    ready = research_store.list_ready(conn)
    assert len(ready) == 1
    assert ready[0]["result_text"] == "the findings"
    assert ready[0]["state"] == "ready"


def test_mark_failed_records_error_and_excludes_from_ready(tmp_path):
    conn = _connect(tmp_path)
    task_id = research_store.insert_queued(conn, "some topic")

    research_store.mark_failed(conn, task_id, "fetch timed out")

    assert research_store.list_ready(conn) == []
    tasks = research_store.list_tasks(conn)
    assert tasks[0]["state"] == "failed"
    assert tasks[0]["error_text"] == "fetch timed out"


def test_mark_consumed_only_affects_ready_tasks_and_records_archive(tmp_path):
    conn = _connect(tmp_path)
    ready_id = research_store.insert_queued(conn, "topic a")
    research_store.mark_ready(conn, ready_id, "findings a")
    failed_id = research_store.insert_queued(conn, "topic b")
    research_store.mark_failed(conn, failed_id, "boom")

    research_store.mark_consumed(conn, [ready_id, failed_id], "briefing_2026-08-05_0900.md")

    tasks = {t["id"]: t for t in research_store.list_tasks(conn)}
    assert tasks[ready_id]["state"] == "consumed"
    assert tasks[ready_id]["archive_file"] == "briefing_2026-08-05_0900.md"
    assert tasks[ready_id]["consumed_at"] is not None
    # A failed task is never silently flipped to consumed just because it
    # was in the same batch — mark_consumed's WHERE clause guards state.
    assert tasks[failed_id]["state"] == "failed"
    assert tasks[failed_id]["archive_file"] is None


def test_mark_consumed_with_empty_list_is_a_noop(tmp_path):
    conn = _connect(tmp_path)
    task_id = research_store.insert_queued(conn, "topic")
    research_store.mark_ready(conn, task_id, "findings")

    research_store.mark_consumed(conn, [], "briefing_2026-08-05_0900.md")

    tasks = research_store.list_tasks(conn)
    assert tasks[0]["state"] == "ready"


def test_list_tasks_is_newest_first(tmp_path):
    conn = _connect(tmp_path)
    first = research_store.insert_queued(conn, "first")
    second = research_store.insert_queued(conn, "second")

    tasks = research_store.list_tasks(conn)
    assert [t["id"] for t in tasks] == [second, first]


def test_a_process_death_between_generate_and_consume_leaves_task_ready(tmp_path):
    """The core durability guarantee: if generate() succeeds and produces
    an archive but the process dies BEFORE mark_consumed runs, the task
    must stay 'ready' — visible and re-includable — never silently lost
    and never falsely marked 'included' in an archive it never reached."""
    conn = _connect(tmp_path)
    task_id = research_store.insert_queued(conn, "topic")
    research_store.mark_ready(conn, task_id, "findings")

    # Simulated crash: generate() ran and archived, but mark_consumed was
    # never called (the line that would call it never executed).

    ready_after_crash = research_store.list_ready(conn)
    assert len(ready_after_crash) == 1
    assert ready_after_crash[0]["id"] == task_id
