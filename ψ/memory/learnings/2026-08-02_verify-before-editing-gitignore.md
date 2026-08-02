---
pattern: Before editing .gitignore for a path, run `git ls-files <path>` first — the correct action depends on whether files under that path are already tracked.
date: 2026-08-02
source: rrr: ai-briefing
concepts: [gitignore, git-tracking, verify-before-edit, process-ordering]
---

# Verify tracked state before editing .gitignore

When a user asks to "gitignore" or "stop tracking" a directory, the right fix branches
on one fact: are files under that path already tracked in git?

- If **nothing tracked yet** → adding the ignore rule is sufficient and complete.
- If **some/most files already tracked** → adding the ignore rule alone creates an
  inconsistent split (old files tracked, new files ignored) without removing anything.
  The user needs to explicitly decide whether to also `git rm --cached` the existing
  files, or abandon the ignore rule and keep everything tracked for consistency.

Check first with `git ls-files <path>` or `git status`, *then* decide the edit — not the
reverse. Editing the ignore file before checking tracked state leads to a discover-after-edit
loop: edit, notice the inconsistency, ask the user, possibly revert. A single upfront
`git ls-files` avoids the wasted round trip.

This generalizes beyond `archives/`: any "please ignore X" request should first answer
"is X already committed, and how much of it?" before touching `.gitignore`.
