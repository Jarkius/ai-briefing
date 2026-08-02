"""Unit tests for briefing.workflow_store.Store — the SQLite schema and
transactional helpers underneath workflow.py. See
tests/test_workflow_contract.py for the higher-level command/invariant
tests; this file covers store-level guarantees the plan calls out
explicitly (schema idempotency, invalid transitions, unique active
Edition, activity atomicity, concurrent writers).
"""

import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from briefing.workflow_store import InvalidTransitionError, Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return Store(db_path=str(tmp_path / "workflow.db"))


def test_schema_creation_is_idempotent(tmp_path):
    """Opening a Store against the same file twice (e.g. server restart)
    must not error on already-existing tables."""
    db_path = str(tmp_path / "workflow.db")
    first = Store(db_path=db_path)
    first.insert_run(trigger="scheduled", delivery_policy="auto_send")
    first.close()

    second = Store(db_path=db_path)
    runs = second._conn.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 1


def test_default_delivery_policy_setting_is_auto_send(store):
    assert store.get_setting("delivery_policy") == "auto_send"


def test_set_setting_overrides_default(store):
    store.set_setting("delivery_policy", "review")
    assert store.get_setting("delivery_policy") == "review"


# ---- invalid transitions ----------------------------------------------------


def test_mark_edition_sending_rejects_already_sent_edition(store):
    edition = store.create_edition(
        source_run_id=None, archive_file="a.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="sent",
    )
    with pytest.raises(InvalidTransitionError):
        store.mark_edition_sending(edition["id"])


def test_mark_edition_dismissed_rejects_sent_edition(store):
    edition = store.create_edition(
        source_run_id=None, archive_file="a.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="sent",
    )
    with pytest.raises(InvalidTransitionError):
        store.mark_edition_dismissed(edition["id"])


def test_consume_research_tasks_rejects_non_ready_task(store):
    task_id = store.insert_research_task("topic")  # state=queued, not ready
    edition = store.create_edition(
        source_run_id=None, archive_file="a.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="needs_review",
    )
    with pytest.raises(InvalidTransitionError):
        store.consume_research_tasks([task_id], edition["id"])


def test_transition_on_missing_edition_raises(store):
    with pytest.raises(InvalidTransitionError):
        store.mark_edition_sending(999)


# ---- unique active edition ---------------------------------------------------


def test_create_edition_supersedes_all_prior_active_editions(store):
    first = store.create_edition(
        source_run_id=None, archive_file="a.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="needs_review",
    )
    store.mark_edition_changes_requested(first["id"])

    second = store.create_edition(
        source_run_id=None, archive_file="b.md", date_str="d",
        part1_html="<p>3</p>", part2_html="<p>4</p>", state="needs_review",
    )

    assert store.get_edition(first["id"])["state"] == "superseded"
    active = store.list_active_editions()
    assert [e["id"] for e in active] == [second["id"]]


def test_import_edition_never_creates_a_second_active_edition(store):
    """Historical import must not supersede a currently-active live
    Edition — a migration run mid-review-cycle must not clobber it."""
    live = store.create_edition(
        source_run_id=None, archive_file="live.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="needs_review",
    )
    store.import_edition(
        archive_file="historical.md", date_str="d",
        part1_html="<p>old</p>", part2_html="<p>old2</p>", state="superseded",
    )
    assert store.get_edition(live["id"])["state"] == "needs_review"


def test_import_edition_is_idempotent_on_rerun(store):
    first = store.import_edition(
        archive_file="historical.md", date_str="d",
        part1_html="<p>old</p>", part2_html="<p>old2</p>", state="superseded",
    )
    second = store.import_edition(
        archive_file="historical.md", date_str="d",
        part1_html="<p>old</p>", part2_html="<p>old2</p>", state="superseded",
    )
    assert first["id"] == second["id"]
    all_rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM editions WHERE archive_file = 'historical.md'"
    ).fetchone()
    assert all_rows["n"] == 1


# ---- activity atomicity ------------------------------------------------------


def test_create_edition_logs_activity_for_creation_and_supersession_together(store):
    first = store.create_edition(
        source_run_id=None, archive_file="a.md", date_str="d",
        part1_html="<p>1</p>", part2_html="<p>2</p>", state="needs_review",
    )
    before = len(store.list_activity())

    store.create_edition(
        source_run_id=None, archive_file="b.md", date_str="d",
        part1_html="<p>3</p>", part2_html="<p>4</p>", state="needs_review",
    )

    after = store.list_activity()
    # One row for the new Edition's creation, one for the old one's supersession.
    assert len(after) == before + 2
    events = {(a["entity_id"], a["event"]) for a in after[before:]}
    assert (first["id"], "superseded") in events


def test_run_status_change_logs_activity(store):
    run_id = store.insert_run(trigger="scheduled", delivery_policy="auto_send")
    before = len(store.list_activity(entity_type="run"))
    store.mark_run_status(run_id, "running", current_phase="collect")
    after = store.list_activity(entity_type="run")
    assert len(after) == before + 1
    assert after[-1]["to_state"] == "running"


# ---- concurrent writers -------------------------------------------------


def test_two_store_instances_on_same_db_path_see_each_others_committed_writes(tmp_path):
    """Simulates cron and the panel opening independent Store instances
    against the same workflow.db (SQLite's own locking, not the workflow
    lock, is what's under test here)."""
    db_path = str(tmp_path / "workflow.db")
    writer_a = Store(db_path=db_path)
    writer_b = Store(db_path=db_path)

    run_id = writer_a.insert_run(trigger="scheduled", delivery_policy="auto_send")

    seen_by_b = writer_b.get_run(run_id)
    assert seen_by_b is not None
    assert seen_by_b["status"] == "queued"


def test_concurrent_run_inserts_from_two_connections_both_persist(tmp_path):
    db_path = str(tmp_path / "workflow.db")
    writer_a = Store(db_path=db_path)
    writer_b = Store(db_path=db_path)

    id_a = writer_a.insert_run(trigger="scheduled", delivery_policy="auto_send")
    id_b = writer_b.insert_run(trigger="manual", delivery_policy="review")

    assert id_a != id_b
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()
    assert count == 2
