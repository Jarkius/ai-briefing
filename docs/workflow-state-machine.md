# Workflow State Machine

Companion spec to `.omx/plans/2026-07-31-workflow-system-redesign.md`. This
document is the single source of truth for the transition table the contract
tests (`tests/test_workflow_contract.py`) and later `workflow.py` /
`workflow_store.py` implement against. Any change to states, commands, or
invariants here must land alongside a contract-test update in the same
change.

## States

### Run

`queued`, `running`, `completed`, `completed_with_warnings`, `failed`,
`interrupted`

### Research task

`queued`, `running`, `ready`, `consumed`, `failed`, `cancelled`

### Edition

`needs_review`, `changes_requested`, `sending`, `sent`, `send_failed`,
`superseded`, `dismissed`

## Commands and transitions

| Command | Preconditions | Effect | Resulting state(s) |
| --- | --- | --- | --- |
| `run_scheduled(delivery_policy=None)` | No other run holds the workflow lock | Creates a `Run(trigger=scheduled)`, sequences Collect → Research → Generate → (Send \| stop for review) | `Run.completed` / `completed_with_warnings` / `failed` |
| `queue_research(inputs, requested_for_edition=None)` | — | Creates one `research_tasks` row per input | `research_tasks.queued` |
| `run_research(trigger="manual")` | No other run holds the workflow lock | Creates a `Run(trigger=manual)`, processes all `queued` research tasks | tasks → `ready` (or `failed`); `Run.completed` |
| `generate_edition(trigger="manual", delivery_policy="review")` | No other run holds the workflow lock; at most one active Edition exists | Creates a `Run`, consumes `ready` research tasks, produces one immutable Edition | New Edition in `needs_review` (review policy) or `sending`→`sent` (auto_send policy); consumed tasks → `consumed` |
| `request_more_research(edition_id, inputs)` | `edition_id` is the current active Edition in `needs_review` or `changes_requested` | Queues new research tasks tied to `requested_for_edition=edition_id` | Edition → `changes_requested`; tasks → `queued` |
| `send_edition(edition_id)` | Edition is `needs_review` or `changes_requested`; no other Edition is `sending` | Sends the Edition's persisted HTML verbatim | `sending` → `sent` or `send_failed` |
| `retry_send(edition_id)` | Edition is `send_failed` | Re-attempts send of the same persisted HTML (never regenerates) | `sending` → `sent` or `send_failed` |
| `dismiss_edition(edition_id)` | Edition is `needs_review`, `changes_requested`, or `send_failed` | Marks Edition as intentionally not sent | `dismissed` |
| `recover_interrupted_runs()` | Called at startup / command entry | Reconciles `running` Runs against workflow-lock ownership/age | Stale `running` → `interrupted` |

A replacement Edition (from a later `generate_edition` call while a prior
Edition is still active and unsent) supersedes the previous one
transactionally: the old Edition moves to `superseded` in the same
transaction that creates the new one.

## Invariants (mirrors plan §"Core Invariants", verbatim numbering)

1. At most one mutating workflow run owns the workflow lock.
2. At most one Edition is active in `needs_review`, `changes_requested`,
   `sending`, or `send_failed`.
3. An Edition's HTML is immutable after creation.
4. Send uses the selected Edition's persisted HTML, never a fresh re-render.
5. A replacement Edition supersedes the previous unsent active Edition
   transactionally.
6. Research results become `consumed` only in the transaction that records
   their resulting Edition.
7. `sent` requires both parts delivered or externally confirmed
   `already_sent`; partial delivery is `send_failed` with detail.
8. Every state transition records one Activity row in the same transaction.
9. User-visible `running` requires a durable active Run plus live
   lock/owner evidence.
10. Historical imports never create `needs_review` automatically.
11. Phase functions (`collector`, `researcher`, `generator`, `sender`) do not
    mutate workflow state or queue files; they accept explicit inputs and
    return results.
12. Archive export is derived from a committed Edition and may be retried
    without regenerating or changing its HTML.

## Soft vs. hard failures

| Phase | Failure mode | Effect on the Run |
| --- | --- | --- |
| Collect | Any exception | Soft — logged, `collect_status` records the error, pipeline continues with existing DB content |
| Research | Any exception per-task | Soft per task — a failed task does not block others; overall Run continues to Generate |
| Generate | Any exception | Hard — stops the Run; no Edition is created; `Run.failed` |
| Send | Partial failure (one part sent, one errored) | Edition → `send_failed` with per-part detail; the Run itself is `completed_with_warnings`, not `failed` |
| Send | Full failure (both parts error) | Edition → `send_failed`; `Run.completed_with_warnings` |

This mirrors current `run.py` behavior exactly: Collect/Research phases are
wrapped in soft try/except (`run.py:34-51`); Generate failure is fatal
(`run.py:59-62`, no `result` means Send is skipped entirely); Send failure
is caught and recorded but does not raise (`run.py:81-83`).

## Delivery policy

- `auto_send` (default): Generate → immediately attempt Send. This is the
  existing behavior end-to-end today (`run.py` always sends unless
  `--dry-run`).
- `review`: Generate stops at `needs_review`. No Send is attempted until an
  explicit `send_edition` command.

Migration must initialize `workflow_settings.delivery_policy = "auto_send"`
so existing scheduled behavior is unchanged after cutover (plan §"Migration
and Cutover" → "Import rules").

## What this replaces (traceability back to current code)

| Legacy authority | Becomes |
| --- | --- |
| `run.py:26-84` sequential phase calls | `workflow.run_scheduled()` |
| `src/panel/app.py` route handlers deciding what to do next | `workflow.*` command calls; routes only submit commands |
| `src/panel/jobs.py` in-memory job status | Execution/polling adapter over durable `Run`/`Edition` rows |
| `src/panel/state.py` (`LAST_GENERATION`, `LAST_RESEARCH_FINDINGS`) | `editions` (active Edition) and `research_tasks` (`ready`, not yet `consumed`) |
| `research_requests.md` checkbox parsing | `research_tasks` table; the markdown file becomes a migration-only compatibility adapter |
| `runs` table in `src/briefing/db.py` | `runs` table in the new `data/workflow.db` (separate file — plan §"Durable Data Model") |
| `archives/*.md` file existence | `editions.archive_file` + `editions.state`, distinguishing active review from historical draft |
| `data/send_status.json` | `editions.send_detail` / `editions.sent_at` |
