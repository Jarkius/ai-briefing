"""Contract tests for the workflow-system redesign
(.omx/plans/2026-07-31-workflow-system-redesign.md, docs/workflow-state-machine.md).

These tests specify `briefing.workflow` / `briefing.workflow_store` before
either module exists. They MUST fail at collection/run time only because of
that missing import — never because of wrong assertion logic — so that
implementing Phase 2/3 turns them green one at a time without rewriting them.

Every test isolates its own `workflow.db` via `tmp_path` + `config.reload()`-
style monkeypatching (same isolation posture as tests/test_db.py) — no shared
state between tests, no real MCP/network/email calls.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

workflow_store = pytest.importorskip(
    "briefing.workflow_store",
    reason="Phase 2 not implemented yet — see .omx/plans/2026-07-31-workflow-system-redesign.md",
)
workflow = pytest.importorskip(
    "briefing.workflow",
    reason="Phase 3 not implemented yet — see .omx/plans/2026-07-31-workflow-system-redesign.md",
)


@pytest.fixture
def store(tmp_path):
    """A fresh workflow_store.Store bound to an isolated SQLite file per test."""
    return workflow_store.Store(db_path=str(tmp_path / "workflow.db"))


@pytest.fixture
def svc(store, monkeypatch):
    """workflow.py wired to the isolated store, with collector/researcher/
    generator/sender collaborators stubbed — no real network/AI/email calls.
    Individual tests override these stubs to inject specific results/failures."""
    svc = workflow.WorkflowService(store=store)
    monkeypatch.setattr(svc, "_collect", lambda: "ok")
    monkeypatch.setattr(svc, "_research", lambda tasks: [(t, f"finding for {t}") for t in tasks])
    monkeypatch.setattr(
        svc,
        "_generate",
        lambda research_findings: {
            "markdown": "# stub",
            "part1_html": "<p>one</p>",
            "part2_html": "<p>two</p>",
            "date_str": "Thursday, July 30, 2026",
            "archive_file": "briefing_2026-07-30_0800.md",
        },
    )
    monkeypatch.setattr(
        svc, "_send", lambda edition: {"part1": "sent", "part2": "sent"}
    )
    return svc


# ---- Invariant 1: single workflow-lock owner --------------------------------


def test_only_one_mutating_run_holds_the_workflow_lock(svc, monkeypatch):
    """A second run_scheduled() while one is in-flight must not create a
    second concurrently-running Run row — it must wait or refuse, never
    interleave writes with the first."""
    entered_generate = []

    def blocking_generate(research_findings):
        entered_generate.append(True)
        # A concurrent call attempted here would prove the lock did not hold.
        with pytest.raises(workflow.WorkflowLockHeldError):
            svc.run_scheduled()
        return {
            "markdown": "# stub", "part1_html": "<p>one</p>", "part2_html": "<p>two</p>",
            "date_str": "Thursday, July 30, 2026", "archive_file": "briefing_2026-07-30_0800.md",
        }

    monkeypatch.setattr(svc, "_generate", blocking_generate)
    svc.run_scheduled()
    assert entered_generate == [True]


# ---- Invariant 2: unique active Edition --------------------------------------


def test_generate_edition_supersedes_prior_unsent_active_edition(svc):
    """Invariant 5 in the plan: a second generate_edition() while the first
    Edition is still needs_review must supersede it, not create two
    concurrently-active Editions (invariant 2)."""
    first = svc.generate_edition(trigger="manual", delivery_policy="review")
    assert first["state"] == "needs_review"

    second = svc.generate_edition(trigger="manual", delivery_policy="review")
    assert second["state"] == "needs_review"
    assert second["id"] != first["id"]

    refreshed_first = svc.store.get_edition(first["id"])
    assert refreshed_first["state"] == "superseded"

    active = svc.store.list_active_editions()
    assert [e["id"] for e in active] == [second["id"]]


# ---- Invariant 3/4: immutable HTML, send uses persisted bytes ----------------


def test_edition_html_is_immutable_and_send_uses_persisted_bytes(svc, monkeypatch):
    """Send must use the Edition's stored HTML verbatim — a fresh call to
    _generate must never be invoked by send_edition."""
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    stored = svc.store.get_edition(edition["id"])
    assert stored["part1_html"] == "<p>one</p>"

    generate_calls = []
    monkeypatch.setattr(svc, "_generate", lambda research_findings: generate_calls.append(1))

    result = svc.send_edition(edition["id"])

    assert generate_calls == []  # send never regenerated content
    assert result["state"] == "sent"


# ---- Invariant 6: research consumed only with a resulting Edition -----------


def test_research_tasks_become_consumed_only_when_their_edition_is_created(svc):
    task_ids = svc.queue_research(["some topic"])
    svc.run_research(trigger="manual")
    ready = svc.store.list_research_tasks(state="ready")
    assert [t["id"] for t in ready] == task_ids

    edition = svc.generate_edition(trigger="manual", delivery_policy="review")

    consumed = svc.store.list_research_tasks(state="consumed")
    assert [t["id"] for t in consumed] == task_ids
    assert svc.store.get_edition(edition["id"])["id"] == edition["id"]


def test_generate_edition_with_no_ready_research_consumes_nothing(svc):
    """Generating without any queued/ready research must not error and must
    leave the research_tasks table untouched."""
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    assert edition["state"] == "needs_review"
    assert svc.store.list_research_tasks(state="consumed") == []


# ---- Invariant 7: sent / partial / failed rules ------------------------------


def test_partial_send_result_marks_edition_send_failed_not_failed_run(svc, monkeypatch):
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    monkeypatch.setattr(
        svc, "_send", lambda e: {"part1": "sent", "part2": "error: smtp down"}
    )

    result = svc.send_edition(edition["id"])

    assert result["state"] == "send_failed"
    assert "smtp down" in result["send_detail"]["part2"]


def test_both_parts_already_sent_counts_as_sent(svc, monkeypatch):
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    monkeypatch.setattr(
        svc, "_send", lambda e: {"part1": "already_sent", "part2": "already_sent"}
    )

    result = svc.send_edition(edition["id"])

    assert result["state"] == "sent"


def test_retry_send_reuses_persisted_html_not_a_fresh_render(svc, monkeypatch):
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    monkeypatch.setattr(svc, "_send", lambda e: {"part1": "error: down", "part2": "error: down"})
    failed = svc.send_edition(edition["id"])
    assert failed["state"] == "send_failed"

    seen_html = []
    monkeypatch.setattr(
        svc,
        "_send",
        lambda e: (seen_html.append(e["part1_html"]), {"part1": "sent", "part2": "sent"})[1],
    )
    retried = svc.retry_send(edition["id"])

    assert retried["state"] == "sent"
    assert seen_html == ["<p>one</p>"]


# ---- Invariant 8: every transition writes one Activity row ------------------


def test_every_transition_records_exactly_one_activity_row(svc):
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")
    before = len(svc.store.list_activity())

    svc.send_edition(edition["id"])

    after = svc.store.list_activity()
    assert len(after) == before + 1
    assert after[-1]["entity_type"] == "edition"
    assert after[-1]["entity_id"] == edition["id"]
    assert after[-1]["to_state"] == "sent"


# ---- Invariant 9: user-visible running requires live lock evidence ----------


def test_stale_running_run_recovers_to_interrupted(svc, store):
    """Simulates a process that crashed mid-run: a `running` Run row exists
    with no live lock owner. recover_interrupted_runs() must flip it to
    `interrupted`, never leave it permanently `running`."""
    run_id = store.insert_run(trigger="scheduled", delivery_policy="auto_send")
    store.mark_run_status(run_id, status="running", current_phase="generate")

    svc.recover_interrupted_runs()

    assert store.get_run(run_id)["status"] == "interrupted"


# ---- Invariant 10: historical imports never auto-create needs_review --------


def test_import_historical_unsent_archive_does_not_create_needs_review(svc, store):
    edition_id = svc.import_historical_edition(
        archive_file="briefing_2026-07-20_0800.md",
        part1_html="<p>old</p>",
        part2_html="<p>old2</p>",
        date_str="Monday, July 20, 2026",
        send_state="never_sent",
    )
    imported = store.get_edition(edition_id)
    assert imported["state"] not in ("needs_review", "changes_requested")
    assert imported["state"] == "superseded"


# ---- Delivery policy: auto_send is the default and preserves current behavior ---


def test_run_scheduled_default_policy_is_auto_send(svc):
    """run.py always sends today unless --dry-run; migration must preserve
    that as the default so cutover doesn't silently start withholding sends."""
    result = svc.run_scheduled()
    assert result["editions"][0]["state"] == "sent"


def test_run_scheduled_review_policy_stops_before_send(svc):
    result = svc.run_scheduled(delivery_policy="review")
    assert result["editions"][0]["state"] == "needs_review"


# ---- Soft vs hard failure semantics (mirrors run.py exactly) ----------------


def test_collect_failure_is_soft_and_pipeline_continues_to_send(svc, monkeypatch):
    monkeypatch.setattr(svc, "_collect", lambda: (_ for _ in ()).throw(RuntimeError("feed down")))
    result = svc.run_scheduled()
    assert result["run"]["collect_status"].startswith("error")
    assert result["editions"][0]["state"] == "sent"


def test_generate_failure_is_hard_and_skips_send_entirely(svc, monkeypatch):
    monkeypatch.setattr(svc, "_generate", lambda research_findings: (_ for _ in ()).throw(RuntimeError("gemini down")))
    result = svc.run_scheduled()
    assert result["run"]["status"] == "failed"
    assert result["editions"] == []


# ---- Preview byte-identity across restart -----------------------------------


def test_active_edition_html_survives_a_fresh_store_instance(tmp_path, svc):
    """Simulates a server restart: a new Store/WorkflowService pointed at the
    same db_path must see byte-identical persisted HTML, not lose it the way
    the current in-memory panel.state.LAST_GENERATION does."""
    edition = svc.generate_edition(trigger="manual", delivery_policy="review")

    reopened_store = workflow_store.Store(db_path=svc.store.db_path)
    reopened = reopened_store.get_edition(edition["id"])

    assert reopened["part1_html"] == "<p>one</p>"
    assert reopened["part2_html"] == "<p>two</p>"
    assert reopened["state"] == "needs_review"
