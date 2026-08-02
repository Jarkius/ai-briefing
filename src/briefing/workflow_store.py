"""SQLite-backed durable store for the editorial workflow.

See docs/workflow-state-machine.md for the state/command contract this
implements, and .omx/plans/2026-07-31-workflow-system-redesign.md
"Durable Data Model" for the schema rationale. Lives in its own
data/workflow.db, deliberately separate from feeds.db (owned by the
vendored MCP server) so lifecycle writes never contend with feed-ingestion
locks (invariant: Phase functions do not mutate workflow state).

Every write that changes entity state also inserts one `activity` row in
the SAME transaction (invariant 8) — callers never call the two
separately; the helper methods below (`_set_run_status`,
`_transition_edition`, `_transition_research_task`) always do both.
"""

import hashlib
import json
import sqlite3
from datetime import datetime


class InvalidTransitionError(RuntimeError):
    """Raised when a command's preconditions aren't met by current state."""


ACTIVE_EDITION_STATES = ("needs_review", "changes_requested", "sending", "send_failed")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def close(self):
        self._conn.close()

    # ---- schema ---------------------------------------------------------

    def _ensure_schema(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL,
                delivery_policy TEXT NOT NULL,
                status TEXT NOT NULL,
                current_phase TEXT,
                collect_status TEXT,
                research_status TEXT,
                generate_status TEXT,
                send_status TEXT,
                resulting_edition_id INTEGER,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                warning_count INTEGER NOT NULL DEFAULT 0,
                error_text TEXT,
                lock_owner TEXT
            );

            CREATE TABLE IF NOT EXISTS research_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_text TEXT NOT NULL,
                input_type TEXT NOT NULL DEFAULT 'topic',
                state TEXT NOT NULL,
                queue_position INTEGER,
                requested_for_edition_id INTEGER,
                result_text TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                consumed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS editions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_edition_id INTEGER,
                source_run_id INTEGER,
                archive_file TEXT UNIQUE,
                state TEXT NOT NULL,
                date_str TEXT,
                part1_html TEXT,
                part2_html TEXT,
                content_hash TEXT,
                send_detail TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                detail TEXT,
                at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()
        self._conn.execute(
            "INSERT OR IGNORE INTO workflow_settings (key, value) VALUES ('delivery_policy', 'auto_send')"
        )
        self._conn.commit()

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _log_activity(self, entity_type: str, entity_id: int, event: str,
                       from_state: str | None, to_state: str | None, detail: str = ""):
        self._conn.execute(
            "INSERT INTO activity (entity_type, entity_id, event, from_state, to_state, detail, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, event, from_state, to_state, detail, self._now()),
        )

    # ---- runs ---------------------------------------------------------

    def insert_run(self, trigger: str, delivery_policy: str, lock_owner: str = "") -> int:
        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO runs (trigger, delivery_policy, status, started_at, updated_at, lock_owner) "
            "VALUES (?, ?, 'queued', ?, ?, ?)",
            (trigger, delivery_policy, now, now, lock_owner),
        )
        self._log_activity("run", cur.lastrowid, "created", None, "queued")
        self._conn.commit()
        return cur.lastrowid

    def get_run(self, run_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def mark_run_status(self, run_id: int, status: str, current_phase: str | None = None, **fields):
        current = self.get_run(run_id)
        from_state = current["status"] if current else None
        set_parts = ["status = ?", "updated_at = ?"]
        values = [status, self._now()]
        if current_phase is not None:
            set_parts.append("current_phase = ?")
            values.append(current_phase)
        for k, v in fields.items():
            set_parts.append(f"{k} = ?")
            values.append(v)
        if status in ("completed", "completed_with_warnings", "failed", "interrupted"):
            set_parts.append("finished_at = ?")
            values.append(self._now())
        values.append(run_id)
        self._conn.execute(f"UPDATE runs SET {', '.join(set_parts)} WHERE id = ?", values)
        self._log_activity("run", run_id, "status_changed", from_state, status)
        self._conn.commit()

    def update_run_fields(self, run_id: int, **fields):
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE runs SET {set_clause}, updated_at = ? WHERE id = ?",
            (*fields.values(), self._now(), run_id),
        )
        self._conn.commit()

    def list_running_runs(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
        return [dict(r) for r in rows]

    # ---- research tasks -------------------------------------------------

    def insert_research_task(self, input_text: str, input_type: str = "topic",
                              requested_for_edition_id: int | None = None) -> int:
        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO research_tasks (input_text, input_type, state, created_at, requested_for_edition_id) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (input_text, input_type, now, requested_for_edition_id),
        )
        self._log_activity("research_task", cur.lastrowid, "created", None, "queued")
        self._conn.commit()
        return cur.lastrowid

    def get_research_task(self, task_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_research_tasks(self, state: str | None = None) -> list[dict]:
        if state is None:
            rows = self._conn.execute("SELECT * FROM research_tasks ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM research_tasks WHERE state = ? ORDER BY id", (state,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _transition_research_task(self, task_id: int, to_state: str, **fields):
        current = self.get_research_task(task_id)
        if current is None:
            raise InvalidTransitionError(f"research_task {task_id} does not exist")
        from_state = current["state"]
        set_parts = ["state = ?"]
        values = [to_state]
        for k, v in fields.items():
            set_parts.append(f"{k} = ?")
            values.append(v)
        values.append(task_id)
        self._conn.execute(
            f"UPDATE research_tasks SET {', '.join(set_parts)} WHERE id = ?", values
        )
        self._log_activity("research_task", task_id, "status_changed", from_state, to_state)

    def mark_research_running(self, task_id: int):
        self._transition_research_task(task_id, "running", started_at=self._now())
        self._conn.commit()

    def mark_research_ready(self, task_id: int, result_text: str):
        self._transition_research_task(task_id, "ready", result_text=result_text, finished_at=self._now())
        self._conn.commit()

    def mark_research_failed(self, task_id: int, error_text: str):
        self._transition_research_task(task_id, "failed", error_text=error_text, finished_at=self._now())
        self._conn.commit()

    def consume_research_tasks(self, task_ids: list[int], edition_id: int):
        """Marks tasks `consumed` — MUST be called inside the same commit as
        the Edition creation it belongs to (invariant 6). Callers use
        `transaction()` to wrap this with `create_edition`."""
        now = self._now()
        for task_id in task_ids:
            current = self.get_research_task(task_id)
            if current is None or current["state"] != "ready":
                raise InvalidTransitionError(
                    f"research_task {task_id} is not 'ready' (state={current['state'] if current else 'missing'})"
                )
            self._conn.execute(
                "UPDATE research_tasks SET state = 'consumed', consumed_at = ? WHERE id = ?",
                (now, task_id),
            )
            self._log_activity("research_task", task_id, "status_changed", "ready", "consumed")

    # ---- editions -------------------------------------------------------

    def get_edition(self, edition_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM editions WHERE id = ?", (edition_id,)).fetchone()
        return dict(row) if row else None

    def list_active_editions(self) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT * FROM editions WHERE state IN "
            f"({','.join('?' for _ in ACTIVE_EDITION_STATES)})",
            ACTIVE_EDITION_STATES,
        ).fetchall()
        return [dict(r) for r in rows]

    def create_edition(self, *, source_run_id: int | None, archive_file: str,
                        date_str: str, part1_html: str, part2_html: str,
                        state: str, consumed_task_ids: list[int] | None = None) -> dict:
        """Creates one immutable Edition, supersedes any prior active
        Edition, and consumes the given research tasks — all in one
        transaction (invariants 2, 3, 5, 6)."""
        content_hash = hashlib.sha256((part1_html + part2_html).encode("utf-8")).hexdigest()
        now = self._now()

        prior_active = self.list_active_editions()

        cur = self._conn.execute(
            "INSERT INTO editions (source_run_id, archive_file, state, date_str, part1_html, "
            "part2_html, content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_run_id, archive_file, state, date_str, part1_html, part2_html,
             content_hash, now, now),
        )
        edition_id = cur.lastrowid
        self._log_activity("edition", edition_id, "created", None, state)

        for prior in prior_active:
            self._conn.execute(
                "UPDATE editions SET state = 'superseded', updated_at = ? WHERE id = ?",
                (now, prior["id"]),
            )
            self._log_activity("edition", prior["id"], "superseded", prior["state"], "superseded")

        if consumed_task_ids:
            self.consume_research_tasks(consumed_task_ids, edition_id)

        self._conn.commit()
        return self.get_edition(edition_id)

    def import_edition(self, *, archive_file: str, date_str: str, part1_html: str,
                        part2_html: str, state: str, send_detail: dict | None = None,
                        sent_at: str | None = None) -> dict:
        """Historical import — always lands in a non-attention state
        (invariant 10). Never supersedes an existing active Edition (a
        migration run must not clobber live review state)."""
        content_hash = hashlib.sha256((part1_html + part2_html).encode("utf-8")).hexdigest()
        now = self._now()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO editions (archive_file, state, date_str, part1_html, part2_html, "
            "content_hash, send_detail, sent_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (archive_file, state, date_str, part1_html, part2_html, content_hash,
             json.dumps(send_detail) if send_detail else None, sent_at, now, now),
        )
        if cur.lastrowid == 0 or cur.rowcount == 0:
            # Idempotent re-import — return the existing row (plan §"Cutover
            # Stages" step 3: running migration twice must be a no-op).
            existing = self._conn.execute(
                "SELECT * FROM editions WHERE archive_file = ?", (archive_file,)
            ).fetchone()
            self._conn.commit()
            return dict(existing)
        self._log_activity("edition", cur.lastrowid, "imported", None, state)
        self._conn.commit()
        return self.get_edition(cur.lastrowid)

    def _transition_edition(self, edition_id: int, to_state: str, **fields):
        current = self.get_edition(edition_id)
        if current is None:
            raise InvalidTransitionError(f"edition {edition_id} does not exist")
        from_state = current["state"]
        set_parts = ["state = ?", "updated_at = ?"]
        values = [to_state, self._now()]
        for k, v in fields.items():
            set_parts.append(f"{k} = ?")
            values.append(v)
        values.append(edition_id)
        self._conn.execute(f"UPDATE editions SET {', '.join(set_parts)} WHERE id = ?", values)
        self._log_activity("edition", edition_id, "status_changed", from_state, to_state)
        return from_state

    def mark_edition_sending(self, edition_id: int):
        current = self.get_edition(edition_id)
        if current is None or current["state"] not in ("needs_review", "changes_requested", "send_failed"):
            raise InvalidTransitionError(
                f"edition {edition_id} cannot be sent from state "
                f"{current['state'] if current else 'missing'}"
            )
        self._transition_edition(edition_id, "sending")
        self._conn.commit()

    def mark_edition_sent(self, edition_id: int, send_detail: dict):
        self._transition_edition(
            edition_id, "sent", send_detail=json.dumps(send_detail), sent_at=self._now()
        )
        self._conn.commit()

    def mark_edition_send_failed(self, edition_id: int, send_detail: dict):
        self._transition_edition(edition_id, "send_failed", send_detail=json.dumps(send_detail))
        self._conn.commit()

    def mark_edition_changes_requested(self, edition_id: int):
        current = self.get_edition(edition_id)
        if current is None or current["state"] not in ("needs_review", "changes_requested"):
            raise InvalidTransitionError(
                f"edition {edition_id} is not awaiting review "
                f"(state={current['state'] if current else 'missing'})"
            )
        self._transition_edition(edition_id, "changes_requested")
        self._conn.commit()

    def mark_edition_dismissed(self, edition_id: int):
        current = self.get_edition(edition_id)
        if current is None or current["state"] not in (
            "needs_review", "changes_requested", "send_failed"
        ):
            raise InvalidTransitionError(
                f"edition {edition_id} cannot be dismissed from state "
                f"{current['state'] if current else 'missing'}"
            )
        self._transition_edition(edition_id, "dismissed")
        self._conn.commit()

    # ---- activity ---------------------------------------------------------

    def list_activity(self, entity_type: str | None = None, entity_id: int | None = None) -> list[dict]:
        query = "SELECT * FROM activity"
        conditions = []
        params: list = []
        if entity_type is not None:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ---- settings -----------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM workflow_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self._conn.execute(
            "INSERT INTO workflow_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()
