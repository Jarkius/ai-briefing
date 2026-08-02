---
pattern: When two ends of a data pipe are built in separate stories, per-story green tests prove parts, not plumbing — add a cross-story flow test the moment the second end lands
date: 2026-07-26
source: rrr: ai-briefing
concepts: [story-slicing, integration-testing, verification, panel]
author: claude-fable-5
machine: Chakkrits-MacBook-Air
privacy: repo-safe
---

# Cross-story pipes need flow tests

Built the control panel in 6 stories, each committed with green tests. S3 (preview/regenerate)
hardcoded `generate(research_findings="")`. S4 (research) produced findings and stored them in
the job result. Both stories verified clean in isolation — and research output silently went
nowhere. The user found it by asking one question: "where will the Research show up?"

The same shape appeared twice more this session:
- Gmail API fallback: code shipped in a commit, but `is_configured()` could never return True
  (no OAuth token on the machine) — capability reported from code-exists, not a live test.
- Windows lock: msvcrt shim built in a prior fix, never wired into the call sites — dead code
  flagged independently by two review lanes weeks later.

**Rule for another Oracle**: when story N produces something story M consumes, the moment M
lands, write one test that drives data from N's output to M's input (and, for capability
claims, one live in-anger exercise on the real machine). "Both ends green" is not "connected".

A cheap enforcement: for platform-conditional or fallback code, add a grep/AST regression test
that asserts the call sites actually route through the shim (5 lines; would have caught the
Windows lock at fix time instead of at review time).
