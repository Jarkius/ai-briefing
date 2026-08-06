---
kind: retrospective
artifact_id: artifact_ai-briefing_retro_session_20260805_research-durability-and-browser
session_id: session_20260805_research-durability-and-browser
project: ai-briefing
created_at: 2026-08-05T15:17:59.638Z
privacy: repo-safe
summary: "User reported a real UX confusion (a checked-off research request with no archive link, and no visible way to trace it) that turned out to be a serious silent data-loss bug: research findings lived only in an in-process global cleared on restart, while the checkbox flipped to done regardless of whether findings survived or were ever included. Consulted Codex for the durability fix's design, implemented a narrow durable research_store.py (PR #13), then built a small research browser (search + detail view, PR #14) per the user's explicit smaller-scope choice over a bigger NotebookLM-style synthesis feature."
---

# Retrospective — 2026-08-05 (session_20260805_research-durability-and-browser)

## Summary

User reported a real UX confusion (a checked-off research request with no archive link, and no visible way to trace it) that turned out to be a serious silent data-loss bug: research findings lived only in an in-process global cleared on restart, while the checkbox flipped to done regardless of whether findings survived or were ever included. Consulted Codex for the durability fix's design, implemented a narrow durable research_store.py (PR #13), then built a small research browser (search + detail view, PR #14) per the user's explicit smaller-scope choice over a bigger NotebookLM-style synthesis feature.

## Focus

Diagnosing and fixing a real data-loss bug in the research pipeline (checkbox marked done, findings never durable, never traced to any edition), then building the user-chosen smaller follow-up (a research browser: search + per-task detail view) rather than the bigger NotebookLM-style option that was also offered.

## Decisions

- Consulted an independent Codex session again (same council pane from the prior loop) before designing the durability fix, specifically asking it to avoid building something 'thrown away in two weeks' given the parallel in-flight workflow-system migration plan — got back a scoped, forward-compatible-but-not-copied design (research_store.py as its own narrow slice, not a premature adoption of workflow_store.py's schema).
- When the user's 'mini NotebookLM' comment implied a possible bigger feature, surfaced it as an explicit AskUserQuestion with three sizes (skip / small browse-only / bigger AI-synthesis) rather than assuming scope and building the larger version — user chose small, confirming the discipline was worth the pause.
- Removed state.py's LAST_RESEARCH_FINDINGS global and its two functions entirely once migration was complete, rather than leaving them as unused dead code alongside the new mechanism — followed through on the 'don't leave unused code' principle even though it required updating 4 existing tests that depended on the old global.

## Open Threads

- PR #14 depends on PR #13 depends on feat/panel-redesign (still unreviewed by the user) — three PRs now stacked, none merged; the dependency chain should be resolved in order once the user reviews the redesign.
- harden/workflow-send-lock (PR #12, from the prior loop) remains open and unreviewed, layered on the separate codex/control-de<redacted> foundation branch (PR #7) — a second, independent stack still pending.
- The research browser is intentionally basic (substring search, no highlighting of matched terms in results) — good enough for a personal research log at current volume, but would need real indexing if the corpus grows substantially.

## Wins

- Didn't take the user's bug report ('should it have the id and link to archive article?') at face value as a small missing-feature ask — traced it all the way to the actual root cause (a silent data-loss bug) by checking every real archive file for the specific research text before writing any fix, which the user's phrasing alone wouldn't have revealed.
- Caught a real upstream parsing bug (textarea paste with no line breaks becomes one giant checkbox line, silently misclassified as a 'topic' and search-queried verbatim) as a SEPARATE, smaller fix alongside the main durability fix — didn't conflate the two, shipped both cleanly scoped.
- Verified the durability guarantee didn't just look right on paper — deliberately triggered a real regenerate that failed (no feed data in the isolated worktree) and confirmed live that the research task correctly stayed 'ready', not falsely marked consumed, exactly the crash-safety property the fix was supposed to provide.
- Recognized a product-scope question (mini NotebookLM) as fundamentally different from a bug fix and paused to ask rather than deciding unilaterally, even while operating under a broad 'use your own judgment' instruction — judgment correctly included knowing which decisions aren't mine to make solo.

## Challenges

- Building three stacked branches/PRs in two consecutive loops (harden/workflow-send-lock, fix/research-result-durability, feat/research-browser) creates a growing review backlog for the user — velocity without a corresponding increase in reviewed/merged work has a real cost, even though each individual piece was scoped and tested well.
- Setting up a fresh isolated worktree's Python venv from scratch was needed three times this session (twice this loop) — each one costs real setup time (uv venv, pip install, mkdir data/, occasional playwright reinstall for screenshots) that a lighter-weight 'just run tests, no live server' path could sometimes skip if I'd assessed upfront whether live verification was actually necessary for a given change.
- The Playwright screenshot verification failed on a Chromium version mismatch in the fresh worktree venv, and rather than resolving it I fell back to curl+grep text verification — a reasonable substitute for this case, but it means the visual polish of the new templates (research_detail.html, the search box styling) was verified structurally, not visually.

## Lessons

### Trace a bug report to its literal root cause before assuming the fix is what the report's phrasing suggests|debugging

Trace a bug report to its literal root cause before assuming the fix is what the report's phrasing suggests|debugging

### Ask before building the bigger option when a comment could mean either a small or large feature|scope

Ask before building the bigger option when a comment could mean either a small or large feature|scope

### Delete now-dead code from a migration immediately, update dependent tests|maintenance

Delete now-dead code from a migration immediately, update dependent tests|maintenance

## AI Diary

This loop started with the user pointing at something that read, on its surface, like a small polish request — 'should it have an id and link to the archive?' It would have been easy to just add an id field and call it done. What made me slow down was that the pasted research text itself looked suspicious: a huge wall of AI-generated prose with no line breaks, sitting in a markdown checklist file that's supposed to hold short topic phrases and URLs. That mismatch was the actual signal, not the user's literal question. Grepping every archive file for that text and finding zero matches was the moment this stopped being a UI nice-to-have and became a real data-loss bug — the checkbox had been lying the whole time, and there was no way for the user to have known without me going and checking. I want to be honest that I don't know how many other checked-off research requests over the project's history suffered the same fate; I only verified this one specific case, not a full audit of every checkbox in research_requests.md, and I didn't do that audit. That's a real gap — I fixed the mechanism going forward but didn't quantify the scope of what may already have been lost. The second half of the loop, the research browser, felt more straightforward, but the moment that mattered was pausing on 'mini NotebookLM' instead of just building toward it. Under a broad autonomous mandate it would have been defensible to just build the bigger, more impressive version — AI synthesis across research history sounds like a better answer to show for the time spent. But that's not what was asked, and guessing bigger when the actual want might be smaller is its own kind of not listening. I asked, got 'small,' and built exactly that. I'd rather under-deliver a scoped ask correctly than over-deliver a guessed one.

## Honest Feedback

- Being handed a vague-sounding follow-up comment ('should it have the id and link... else, a mini version of notebooklm') and treating it as two separate signals — a concrete small bug (missing link) and an open-ended bigger idea worth asking about — rather than one blended request, produced a cleaner outcome than trying to satisfy both readings at once would have.
- Consulting the same Codex council pane a second time, referencing the first consultation's guidance explicitly ('given your prior guidance to harden incrementally'), kept the conversation efficient — it didn't need to re-establish context, and its answer built directly on the established constraint set (don't touch run.py/generator, stay forward-compatible with the eventual workflow migration) rather than re-deriving it from scratch.
- Three unmerged, stacked PRs opened across two loops without any merge yet is a real velocity-versus-throughput tension worth naming plainly — each PR is well-tested and independently reviewable, but the user now has a growing queue of things to actually look at, which partially offsets the value of moving fast solo overnight.

## Next Steps

- Resolve the stacked-PR backlog: get user review on feat/panel-redesign first (root of both PR chains), then work down each stack (PR #12 harden -> #7 workflow plan; PR #13 durability -> #14 browser).
- Open question, not yet answered: should future isolated-worktree loops default to skipping live Playwright/browser verification when curl+grep can adequately confirm the change (to reduce setup overhead), reserving screenshots for changes where visual layout itself is the risk?
- Continue evolving the project per the standing 'use your own judgment' instruction — next candidate: review PR #9 (social-post-email) and PR #7 (workflow plan) for anything safely actionable, as flagged at the end of the prior loop and not yet revisited.
