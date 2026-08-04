"""Workflow service: the single command API scheduled and manual triggers
both call. See docs/workflow-state-machine.md for the full transition
table and invariants this implements.

Command methods (`run_scheduled`, `run_research`, `generate_edition`) own
sequencing and hold the workflow lock for their duration (invariant 1).
`send_edition` / `retry_send` / `dismiss_edition` rely on workflow_store's
own per-Edition state checks instead (invariant 2 — "no other Edition is
sending" — is enforced by the Edition's own state machine, not the run
lock, since two runs are never mutating the SAME Edition concurrently by
construction).

Phase collaborators (`_collect`, `_research`, `_generate`, `_send`) are
thin wrappers over the existing collector/researcher/generator/sender
modules (invariant 11: those modules stay independent and are not touched
here beyond what's necessary to call them). Tests monkeypatch these four
methods directly to avoid real network/AI/email calls — see
tests/test_workflow_contract.py.
"""

import contextlib
import json
import os

from . import config
from .workflow_store import InvalidTransitionError, Store  # noqa: F401  (re-exported)

try:
    import fcntl

    def _try_lock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _try_lock_fd(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError(str(e)) from e

    def _unlock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class WorkflowLockHeldError(RuntimeError):
    """Raised when a mutating workflow command is attempted while another
    (same-process or cross-process) mutating command already holds the
    workflow lock. Callers should treat this as "try again shortly", not
    a hard failure — mirrors mcp_client.LockHeldError's posture."""


class WorkflowGenerateFailedError(RuntimeError):
    """Generate failed for a standalone generate_edition() call. Run's
    status is already recorded as 'failed' in the store before this is
    raised — callers only need to catch this to render an error, not to
    finish recording the failure themselves."""


SEND_OK_STATES = {"sent", "already_sent"}


class WorkflowService:
    def __init__(self, store: Store, lock_path: str | None = None):
        self.store = store
        self.lock_path = lock_path or config.WORKFLOW_LOCK_PATH
        self._in_process_locked = False

    # ---- workflow lock (invariant 1) -------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        if self._in_process_locked:
            raise WorkflowLockHeldError("workflow lock already held in this process")
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
        try:
            _try_lock_fd(fd)
        except BlockingIOError:
            os.close(fd)
            raise WorkflowLockHeldError(
                f"{self.lock_path} is held by another process"
            ) from None
        self._in_process_locked = True
        try:
            yield
        finally:
            self._in_process_locked = False
            with contextlib.suppress(OSError):
                _unlock_fd(fd)
            os.close(fd)

    # ---- edition hydration (send_detail is stored as JSON text) ---------

    @staticmethod
    def _hydrate(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        if row.get("send_detail"):
            row["send_detail"] = json.loads(row["send_detail"])
        return row

    # ---- phase collaborators (thin wrappers; tests monkeypatch these) ---

    def _collect(self) -> str:
        from datetime import datetime

        from . import collector, db

        conn = db.connect()
        try:
            legacy_run_id = db.insert_run(conn, source="workflow", started_at=datetime.now().isoformat())
            status = collector.run(legacy_run_id, conn)
            db.update_run(conn, legacy_run_id, collect_status=status)
            return status
        finally:
            conn.close()

    def _research(self, inputs: list[str]) -> list[tuple[str, str]]:
        import asyncio

        from . import mcp_client, researcher

        async def _run():
            async with mcp_client.McpSession() as session:
                return [(text, await researcher.research_one(session, text)) for text in inputs]

        with mcp_client.mcp_lock(retry_seconds=120):
            return asyncio.run(_run())

    def _generate(self, research_findings: str) -> dict:
        from . import db, generator

        conn = db.connect()
        try:
            return generator.generate(conn, research_findings=research_findings)
        finally:
            conn.close()

    def _send(self, edition: dict) -> dict:
        from . import sender

        return sender.send_two_part_briefing(
            edition["part1_html"], edition["part2_html"], edition["date_str"]
        )

    # ---- research commands -----------------------------------------------

    def queue_research(self, inputs: list[str], requested_for_edition: int | None = None) -> list[int]:
        return [
            self.store.insert_research_task(text, requested_for_edition_id=requested_for_edition)
            for text in inputs
        ]

    def request_more_research(self, edition_id: int, inputs: list[str]) -> list[int]:
        task_ids = self.queue_research(inputs, requested_for_edition=edition_id)
        self.store.mark_edition_changes_requested(edition_id)
        return task_ids

    def run_research(self, trigger: str = "manual") -> dict:
        with self._lock():
            policy = self.store.get_setting("delivery_policy", "auto_send")
            run_id = self.store.insert_run(trigger=trigger, delivery_policy=policy)
            self.store.mark_run_status(run_id, "running", current_phase="research")

            queued = self.store.list_research_tasks(state="queued")
            for task in queued:
                self.store.mark_research_running(task["id"])

            try:
                results = self._research([t["input_text"] for t in queued]) if queued else []
            except Exception as e:
                for task in queued:
                    self.store.mark_research_failed(task["id"], str(e)[:500])
                self.store.update_run_fields(run_id, research_status=f"error: {e}")
                self.store.mark_run_status(run_id, "failed", error_text=str(e)[:500])
                raise

            result_map = dict(results)
            for task in queued:
                self.store.mark_research_ready(task["id"], result_map.get(task["input_text"], ""))

            status = f"ok ({len(queued)} processed)" if queued else "ok (nothing pending)"
            self.store.update_run_fields(run_id, research_status=status)
            self.store.mark_run_status(run_id, "completed")

        return {"run": self.store.get_run(run_id), "processed": len(queued)}

    # ---- generate / edition commands -------------------------------------

    def generate_edition(self, trigger: str = "manual", delivery_policy: str = "review") -> dict:
        with self._lock():
            run_id = self.store.insert_run(trigger=trigger, delivery_policy=delivery_policy)
            self.store.mark_run_status(run_id, "running", current_phase="generate")

            ready_tasks = self.store.list_research_tasks(state="ready")
            combined_findings = "\n\n".join(
                t["result_text"] for t in ready_tasks if t.get("result_text")
            )

            try:
                gen = self._generate(combined_findings)
            except Exception as e:
                self.store.update_run_fields(run_id, generate_status=f"error: {e}")
                self.store.mark_run_status(run_id, "failed", error_text=str(e)[:500])
                raise WorkflowGenerateFailedError(str(e)) from e

            edition = self.store.create_edition(
                source_run_id=run_id,
                archive_file=gen["archive_file"],
                date_str=gen["date_str"],
                part1_html=gen["part1_html"],
                part2_html=gen["part2_html"],
                state="needs_review",
                consumed_task_ids=[t["id"] for t in ready_tasks],
            )
            self.store.update_run_fields(run_id, generate_status="ok", resulting_edition_id=edition["id"])
            self.store.mark_run_status(run_id, "completed")

        if delivery_policy == "auto_send":
            return self.send_edition(edition["id"])
        return self._hydrate(edition)

    def run_scheduled(self, delivery_policy: str | None = None) -> dict:
        with self._lock():
            policy = delivery_policy or self.store.get_setting("delivery_policy", "auto_send")
            run_id = self.store.insert_run(trigger="scheduled", delivery_policy=policy)

            self.store.mark_run_status(run_id, "running", current_phase="collect")
            try:
                collect_status = self._collect()
            except Exception as e:
                collect_status = f"error: {e}"
            self.store.update_run_fields(run_id, collect_status=collect_status)

            self.store.mark_run_status(run_id, "running", current_phase="research")
            queued = self.store.list_research_tasks(state="queued")
            for task in queued:
                self.store.mark_research_running(task["id"])
            failed_ids: set[int] = set()
            try:
                results = self._research([t["input_text"] for t in queued]) if queued else []
                research_status = f"ok ({len(queued)} processed)" if queued else "ok (nothing pending)"
            except Exception as e:
                results = []
                research_status = f"error: {e}"
                for task in queued:
                    self.store.mark_research_failed(task["id"], str(e)[:500])
                    failed_ids.add(task["id"])
            result_map = dict(results)
            for task in queued:
                if task["id"] not in failed_ids:
                    self.store.mark_research_ready(task["id"], result_map.get(task["input_text"], ""))
            self.store.update_run_fields(run_id, research_status=research_status)

            self.store.mark_run_status(run_id, "running", current_phase="generate")
            ready_tasks = self.store.list_research_tasks(state="ready")
            combined_findings = "\n\n".join(
                t["result_text"] for t in ready_tasks if t.get("result_text")
            )
            try:
                gen = self._generate(combined_findings)
            except Exception as e:
                self.store.update_run_fields(run_id, generate_status=f"error: {e}")
                self.store.mark_run_status(run_id, "failed", error_text=str(e)[:500])
                return {"run": self.store.get_run(run_id), "editions": []}

            edition = self.store.create_edition(
                source_run_id=run_id,
                archive_file=gen["archive_file"],
                date_str=gen["date_str"],
                part1_html=gen["part1_html"],
                part2_html=gen["part2_html"],
                state="needs_review",
                consumed_task_ids=[t["id"] for t in ready_tasks],
            )
            self.store.update_run_fields(run_id, generate_status="ok", resulting_edition_id=edition["id"])
            self.store.mark_run_status(run_id, "completed")

        if policy == "auto_send":
            edition = self.send_edition(edition["id"])
        else:
            edition = self._hydrate(edition)
        return {"run": self.store.get_run(run_id), "editions": [edition]}

    # ---- send commands ------------------------------------------------

    def send_edition(self, edition_id: int) -> dict:
        """Serialized by the workflow lock for its full duration (invariant
        1) — `mark_edition_sending`'s own state check is read-then-write
        with no transaction wrapping the pair, so two concurrent send
        commands for the same Edition could both read 'needs_review' before
        either writes 'sending', and both would proceed to send. Holding
        the lock across the external send too (not just the transition)
        keeps the system-wide invariant simple: only one send is ever in
        flight. The store writes themselves stay short (each commits
        immediately); only the network call in between is long-running.

        This method is only ever called from outside an existing
        self._lock() block — generate_edition/run_scheduled call it after
        their own `with self._lock():` has already exited — so this never
        nests (WorkflowLockHeldError would otherwise fire on re-entry)."""
        with self._lock():
            self.store.mark_edition_sending(edition_id)  # raises InvalidTransitionError if not eligible
            edition = self.store.get_edition(edition_id)
            try:
                send_result = self._send(edition)
            except Exception as e:
                send_result = {"part1": f"error: {e}", "part2": f"error: {e}"}

            if set(send_result.values()) <= SEND_OK_STATES:
                self.store.mark_edition_sent(edition_id, send_result)
            else:
                self.store.mark_edition_send_failed(edition_id, send_result)
        return self._hydrate(self.store.get_edition(edition_id))

    def retry_send(self, edition_id: int) -> dict:
        current = self.store.get_edition(edition_id)
        if current is None or current["state"] != "send_failed":
            raise InvalidTransitionError(
                f"edition {edition_id} is not send_failed "
                f"(state={current['state'] if current else 'missing'})"
            )
        return self.send_edition(edition_id)

    def dismiss_edition(self, edition_id: int) -> dict:
        self.store.mark_edition_dismissed(edition_id)
        return self._hydrate(self.store.get_edition(edition_id))

    # ---- migration / recovery ------------------------------------------

    _IMPORT_STATE_MAP = {
        "sent": "sent",
        "partial": "send_failed",
        "error": "send_failed",
        "never_sent": "superseded",
    }

    def import_historical_edition(self, *, archive_file: str, part1_html: str, part2_html: str,
                                   date_str: str, send_state: str = "never_sent") -> int:
        """Historical import — always lands non-attention (invariant 10;
        see plan "Migration and Cutover" -> Import rules: 'Historical
        unsent archives import as superseded/Older draft, never
        needs_review')."""
        state = self._IMPORT_STATE_MAP.get(send_state, "superseded")
        edition = self.store.import_edition(
            archive_file=archive_file, date_str=date_str,
            part1_html=part1_html, part2_html=part2_html, state=state,
        )
        return edition["id"]

    def recover_interrupted_runs(self) -> list[int]:
        """Reconciles `running` Runs against live lock ownership (invariant
        9). This process-local implementation has no cross-process owner
        registry yet — any Run still `running` when this is called is, by
        definition, not actively owned by the calling process, so it is
        always flipped to `interrupted`. Call at process startup."""
        recovered = []
        for run in self.store.list_running_runs():
            self.store.mark_run_status(run["id"], "interrupted")
            recovered.append(run["id"])
        return recovered
