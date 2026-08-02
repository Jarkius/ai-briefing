---
pattern: Mocked tests proving a network-send function "works" do not prove it is safe to retry — verify with a real call and an independent observation channel before trusting retry/fallback logic around it
date: 2026-08-02
source: rrr: ai-briefing
concepts: [idempotency, retry-safety, verification, smtp, integration-testing]
author: claude-fable-5
machine: unknown
privacy: repo-safe
---

# Verify network sends with an independent channel, not the sender's own return value

Built a Gmail API fallback for `sender.py`, wrote unit tests with mocked `smtplib`/`imaplib`
calls, all green. Then did a live real (non-dry-run) send to confirm end-to-end delivery — and
IMAP showed 4 emails instead of 2. Root cause: `with smtplib.SMTP_SSL(...) as server:` calls
`server.quit()` on context exit; `smtplib`'s `__exit__` only swallows `SMTPServerDisconnected`
during that QUIT, not `ConnectionResetError`. On a network with intermittent TLS resets, the
actual `sendmail()` had already succeeded, but the QUIT-time reset propagated as if the whole
call failed — and the retry wrapper (`_with_retry`) faithfully resent the identical email.

The mocked tests never exercised this because the mock's `__exit__` didn't raise after a
successful `sendmail()` — the test's model of the dependency was simpler than the real one.
This is the second variant of "retry-without-idempotency-guard" in this codebase's history
(see `session-metrics.md`/prior retros for the first); two occurrences of the same shape is
worth naming as a pattern rather than treating each as an isolated bug.

**Rule for another Oracle**: for any function that both (a) has side effects on a remote system
and (b) is wrapped in automatic retry, mocked unit tests are necessary but not sufficient. Before
trusting it, do one real call and check success via a channel independent of the function's own
return value (e.g. the actual mailbox via IMAP, not the "Send result: sent" log line the sender
itself produced) — and specifically test what happens when the operation succeeds but the
*cleanup/acknowledgment* step fails, since that's the gap most retry wrappers don't cover.

A cheap enforcement: any `with <network-client>(...) as x:` block wrapped in a retry loop should
track a local "did the actual side-effecting call complete" flag and treat a post-completion
exception as a warning, not a retry trigger — the fix applied here, and worth checking for
anywhere else in the codebase the same `with ... as server:` + retry shape appears (e.g. IMAP,
future API clients).
