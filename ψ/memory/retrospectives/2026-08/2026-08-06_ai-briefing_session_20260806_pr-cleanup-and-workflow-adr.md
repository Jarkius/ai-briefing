---
kind: retrospective
artifact_id: artifact_ai-briefing_retro_session_20260806_pr-cleanup-and-workflow-adr
session_id: session_20260806_pr-cleanup-and-workflow-adr
project: ai-briefing
created_at: 2026-08-06T15:25:35.419Z
privacy: repo-safe
summary: "Resolved the full open-PR backlog from prior sessions: closed PR #9 as fully redundant (byte-identical content already on main via #11), merged the clean dependency chain (#14->#13->feat/panel-redesign->#15->main, 442/442 tests passing), then investigated and wrote a real ADR (docs/adr/0001-defer-workflow-state-machine-rewrite.md) explaining why the codex/control-de<redacted> workflow rewrite (PR #7/#12) was deferred rather than merged, closing both with that reasoning attached."
---

# Retrospective — 2026-08-06 (session_20260806_pr-cleanup-and-workflow-adr)

## Summary

Resolved the full open-PR backlog from prior sessions: closed PR #9 as fully redundant (byte-identical content already on main via #11), merged the clean dependency chain (#14->#13->feat/panel-redesign->#15->main, 442/442 tests passing), then investigated and wrote a real ADR (docs/adr/0001-defer-workflow-state-machine-rewrite.md) explaining why the codex/control-de<redacted> workflow rewrite (PR #7/#12) was deferred rather than merged, closing both with that reasoning attached.

## Focus

Clearing the ai-briefing PR backlog (5 open PRs at loop start) and formally recording, not silently dropping, the decision to defer the workflow-state-machine rewrite

## Decisions

- Closed PR #9 after diffing generator.py/sender.py/researcher.py/db.py byte-for-byte against main and confirming zero real difference, rather than trusting the PR title/age
- Merged #14->#13->feat/panel-redesign->#15->main in strict dependency order, running the full test suite (442/442) after each merge before proceeding to the next, rather than merging the whole stack at once
- Deferred PR #7/#12 (workflow.py/workflow_store.py rewrite) instead of merging, because it was never wired into run.py/app.py and now conflicts with main on research_requests.md/config.py -- research_store.py (already merged) solved the narrower research-durability problem this plan was also targeting, with far less surface area

## Open Threads

- User has not yet given visual sign-off on the merged panel redesign itself -- it shipped to main without a live click-through/screenshot review
- docs/adr/0001-defer-workflow-state-machine-rewrite.md defines concrete revisit triggers (send/delivery state divergence after restart, run.py/app.py phase-sequencing drift) -- if any fire, resume from the preserved plan doc rather than re-deriving the design

## Wins

- Verified PR #9's redundancy via direct content diff instead of trusting branch metadata, avoiding either a wasted merge or an unexamined stale PR sitting in backlog
- Used git merge-tree before every real merge in the clean chain -- zero surprise conflicts, zero manual conflict resolution needed
- Wrote a real ADR with concrete revisit triggers for the deferred workflow rewrite instead of just closing the PR with a comment -- the reasoning is now discoverable, not just implied by a closed-PR event

## Challenges

- Committed archive/memory-sync data directly on main before checking this repo's own standing 'always work on a feature branch' rule -- caught it before push and surfaced the contradiction to the user rather than pushing silently, but the check happened after the commit, not before it
- Relied on gh pr view's mergeStateStatus/mergeable fields, which return UNKNOWN transiently right after pushes/base changes -- had to re-check with a fresh call before acting rather than trusting the first read
- Re-derived the 'why deferred' reasoning for PR #7 from scratch via fresh git show/diff calls when the user asked, rather than checking whether a prior session had already written it down somewhere retrievable -- fast here, but would be wasted work on a costlier investigation

## Lessons

### Check repo-specific standing rules (like never-commit-to-main) before the git action that would violate them, not after catching it in the next step|process

Check repo-specific standing rules (like never-commit-to-main) before the git action that would violate them, not after catching it in the next step|process

### Verify PR redundancy claims via direct content diff (diff on git show output), not via branch metadata or commit-message titles alone|collaboration

Verify PR redundancy claims via direct content diff (diff on git show output), not via branch metadata or commit-message titles alone|collaboration

### When deferring a substantial unmerged feature branch, write a real ADR with concrete revisit triggers instead of just closing the PR with a comment|architecture

When deferring a substantial unmerged feature branch, write a real ADR with concrete revisit triggers instead of just closing the PR with a comment|architecture

## AI Diary

This loop started mid-way through a compacted conversation, so the first job was reconstructing what state 5 open PRs and one dirty working tree actually represented before touching any of it. The PR #9 investigation is the part I'm most confident about -- rather than trusting that an old-looking branch was stale, I diffed the actual file contents against main and found they were byte-identical, which turned 'is this safe to close' from a guess into a fact. The uncomfortable part of this session is the direct-to-main commit. I have a specific, named rule for this exact repo about never committing to main because the user works across two computers, and I still ran git commit on main without checking that rule first -- I only caught it when deciding whether to push. Catching it before push and asking the user rather than silently pushing was the right recovery, but recovery isn't the same as prevention, and I want to be honest that the check happened reactively, not proactively, which is the same shape of mistake as an earlier git-reset incident in this project's memory. The ADR-writing part felt genuinely valuable -- when the user asked 'why deferred,' I didn't answer from memory of the prior compacted session, I went back and re-read the actual 415-line plan document and re-diffed the actual conflicts, so the explanation was grounded in current reality rather than a stale recollection. Writing that down permanently as docs/adr/0001 rather than just closing PR #7 with a comment is the kind of promotion-to-procedural-memory this project's own memory hierarchy calls for, and I think it will save real re-investigation time if this question comes up a third time.

## Honest Feedback

- The direct-to-main commit was a real process violation, not a theoretical one -- I have a named rule for this exact repo and still violated it before checking. The fact that the content was low-risk (archives, memory sync) doesn't make the process gap acceptable for a higher-risk case.
- gh pr view's mergeStateStatus is asynchronous and returns UNKNOWN right after certain operations -- treating that as meaningful signal on first read would be a mistake; it needs a re-check, which I don't have written down anywhere as a standing rule yet.
- Writing the ADR required re-reading the entire original 415-line plan document from scratch to accurately summarize the deferral reasoning -- if this exact question gets asked a third time and the ADR still isn't sufficient to answer it, that's a sign the ADR failed at its one job.

## Next Steps

- Get the user's visual sign-off on the panel redesign now that it's live on main
- Watch for the ADR's revisit triggers (delivery-state divergence, run.py/app.py phase-sequencing drift) before reconsidering the workflow rewrite
