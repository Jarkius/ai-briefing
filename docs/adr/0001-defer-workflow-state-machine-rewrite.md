# ADR 0001: Defer the workflow state-machine rewrite (PR #7 / #12)

**Status**: Deferred (not rejected)
**Date**: 2026-08-06
**Source**: `.omx/plans/2026-07-31-workflow-system-redesign.md` on the closed
`codex/control-desk-redesign-plans` branch (PR #7), hardened by PR #12.

## Decision

Close PR #7 (`codex/control-desk-redesign-plans` → `main`) and PR #12
(`harden/workflow-send-lock` → #7) without merging. Keep this ADR as the
durable record of the design reasoning, in case the underlying pain
resurfaces.

## Context

The plan proposed replacing scattered per-subsystem state — `run.py`'s
scheduled path, `panel/app.py`'s manual routes, `panel/state.py`'s
in-memory globals, `research_requests.md` as a markdown queue, and
`archives/` files as ad-hoc history — with one durable command path:
`src/briefing/workflow.py` + `data/workflow.db`, used by both scheduled and
manual triggers, with explicit state transitions (`queued` → `running` →
`needs_review` → `sending` → `sent`, etc.) instead of each part of the code
inferring lifecycle state independently.

The branch delivered real foundation work — `workflow.py` (352 lines) and
`workflow_store.py` (418 lines), with contract tests — plus PR #12's fix for
a genuine TOCTOU race (the send lock wasn't held across the full external
send call).

## Why deferred, not merged

1. **Not wired in.** Neither `run.py` nor `src/panel/app.py` on that branch
   actually call into `workflow.py` — it's plumbing without the switch-over.
   Merging it as-is would add a second, unused state-tracking system next to
   the one actually running the app.
2. **Conflicts with `main`.** The panel-redesign work merged the same week
   (PR #13/#14/#15) added `src/briefing/research_store.py` — a durable,
   SQLite-backed fix for the exact same underlying problem this plan calls
   out (`research_requests.md` acting as a queue database). Both branches
   independently touched `research_requests.md` and `config.py`, producing
   real merge conflicts.
3. **Narrower fix already shipped.** `research_store.py` solves the research
   durability half of the motivating problem with much less surface area
   than a full `workflow.db` state machine — no new command API, no
   migration/shadow-projection rollout, no phase-sequencing rewrite.
4. **Foundational rewrites need integration, not just plumbing.** Landing a
   parallel state authority underneath a live, actively-changing panel is
   the kind of risk this project's own operating discipline treats as
   requiring a full plan + staged rollout (shadow projection, parity report,
   explicit legacy-write cutover) — the original plan already specified
   this migration strategy; none of it has been executed yet.

## What's still true / worth keeping

The architectural reasoning holds up independent of whether this exact
implementation lands:

- One command path for scheduled + manual triggers, rather than duplicated
  phase-sequencing logic in `run.py` and `app.py`.
- Immutable edition revisions (regeneration creates a new revision; old
  drafts don't silently change).
- Explicit, validated state transitions instead of state inferred from file
  existence / in-memory globals that vanish on restart.
- A running state without a live process owner after restart becomes
  `interrupted`, never silently stays `running`.

## Revisit triggers

Reopen this question if any of the following recur:

- Delivery/send state (`data/send_status.json`) or run history (`runs` in
  `db.py`) shows the same "diverges from reality after a crash/restart"
  failure mode that motivated `research_store.py`.
- `run.py`'s scheduled path and `panel/app.py`'s manual routes drift further
  apart in how they sequence collect/research/generate/send, to the point
  duplicated logic causes a real bug (not just duplication as a code smell).
- The panel needs to answer "what happened before or across a restart?" for
  something beyond research findings, and a one-off durable store (like
  `research_store.py`) stops being the cheaper fix.

If reopened, start from the full plan
(`docs/adr/0001-workflow-system-redesign-plan.md`, archived alongside this
ADR) rather than re-deriving the design from scratch — the transition
matrix, data model, and migration strategy were already worked out in
detail.

## Consequences

- No `data/workflow.db` or `workflow.py` command API exists in `main`.
- Research durability is covered by `research_store.py`; other subsystems
  (`send_status.json`, `runs` table, in-memory `state.py` globals) remain on
  their current, narrower persistence approaches until a revisit trigger
  fires.
- The TOCTOU send-lock fix from PR #12 is not needed independently — it only
  applied to `workflow.py`'s `send_edition()`, which isn't in `main`.
