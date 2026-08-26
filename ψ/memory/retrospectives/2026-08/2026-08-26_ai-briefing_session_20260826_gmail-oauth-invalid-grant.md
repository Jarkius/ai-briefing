---
kind: retrospective
artifact_id: artifact_ai-briefing_retro_session_20260826_gmail-oauth-invalid-grant
session_id: session_20260826_gmail-oauth-invalid-grant
project: ai-briefing
created_at: 2026-08-26T16:10:50.796Z
privacy: repo-safe
summary: "Diagnosed today's silent daily-briefing failure (laptop-sleep-induced network flakiness), found the real send-blocker (dead Gmail SMTP app password), migrated to Gmail API OAuth and published the consent screen to production, fixed a stale 7-day-expiry-warning code assumption, then hit a second real invalid_grant token death whose root cause is still unconfirmed."
---

# Retrospective — 2026-08-26 (session_20260826_gmail-oauth-invalid-grant)

## Summary

Diagnosed today's silent daily-briefing failure (laptop-sleep-induced network flakiness), found the real send-blocker (dead Gmail SMTP app password), migrated to Gmail API OAuth and published the consent screen to production, fixed a stale 7-day-expiry-warning code assumption, then hit a second real invalid_grant token death whose root cause is still unconfirmed.

## Focus

AI briefing daily send pipeline — diagnosing why it stopped sending, migrating to Gmail API OAuth, and chasing a second invalid_grant token death

## Decisions

- Moved from Gmail SMTP app-password auth to Gmail API OAuth as primary transport, and published the OAuth consent screen to production to remove Testing mode's 7-day refresh-token cap — the app password itself was rejected by Google (BadCredentials) independent of the network issue that triggered the original investigation

## Open Threads

- Root cause of the second invalid_grant (token died <home>2 days in, well under the 7-day Testing cap and while already in production) is unconfirmed — user was checking Google Account 'Recent security activity' but hasn't reported findings back

## Wins

- Diagnosed the silent daily-briefing failure to laptop-sleep-induced DNS/network flakiness across all 3 LLM providers via briefing.log timestamps, then manually re-ran the pipeline successfully once network was clear
- Found the real send-blocker (Gmail SMTP app password rejected, 535 BadCredentials) via a raw smtplib login test rather than trusting the pipeline's wrapped error text
- Completed Gmail API OAuth setup, published the consent screen to production, and verified end-to-end by resending the actual generated briefing (not a synthetic test) via the new transport
- Fixed gmail_api.py's token_status() to stop hardcoding a Testing-mode 7-day countdown once GMAIL_OAUTH_PUBLISHED is set, added a matching test, verified against the full 443-test suite with zero regressions

## Challenges

- The OAuth token died a second time with invalid_grant only <home>2 days after minting — well short of the 7-day Testing limit and with the app already 'In production' — so the actual root cause is still unknown
- Google Cloud Console's OAuth consent screen UI has moved (Testing/Production toggle now under the 'Audience' tab) and official docs didn't clearly state the new location — needed 3 separate Google support pages to confirm
- gcloud CLI's only OAuth-brand command (iap oauth-brands) turned out deprecated and already shut down (March 2026) — a dead end that cost a docs lookup before ruling it out

## Lessons

### Google's invalid_grant error has multiple distinct causes — don't reuse the first hypothesis just because it fit the first failure

Google's invalid_grant error has multiple distinct causes — don't reuse the first hypothesis just because it fit the first failure

### Verify auth errors with a direct raw client call, not just the pipeline's wrapped error text

Verify auth errors with a direct raw client call, not just the pipeline's wrapped error text

### Console-only OAuth state with no API surface needs a human-set config flag, not silent code assumptions

Console-only OAuth state with no API surface needs a human-set config flag, not silent code assumptions

## AI Diary

I started this session assuming the ai-briefing break was a self-contained network problem — laptop asleep, DNS flaked, retries exhausted — and that story held up fine on the first pass. But once I dug into the send failure specifically, it became a much longer chain than expected: dead app password, then OAuth setup, then a fake 7-day-warning bug, then a second real token death I still can't explain. The uncomfortable truth is I initially reached for the same '7-day Testing cap' explanation for the second invalid_grant almost by reflex, even though the numbers didn't fit (2 days, not 7) and I'd already disproven that exact theory for this same token earlier in the session. I only caught it because I checked actual timestamps in briefing.log instead of pattern-matching the error text to the story already in my head. I pushed the user toward checking Google's account security logs rather than guessing further, which feels like the right call, but it means this session ends with a real open question instead of a clean resolution — the token could die again tomorrow and I wouldn't yet know why.

## Honest Feedback

- The generic invalid_grant message gave zero signal about which of several plausible causes was real — had to reconstruct a timeline from log timestamps and a manual credential-refresh reproduction just to rule out the most obvious (wrong) explanation
- Google Cloud Console's OAuth consent screen has been restructured (Branding/Audience/Data Access/Verification Center tabs) since whatever docs I could fetch reflected clearly — needed the user's own description of what they saw ('Audience / Publishing status: In production') to actually confirm state, rather than any doc
- There's a real gap between what token_status() assumed (permanent Testing mode) and reality once the app got published — that kind of Console-only state with no API surface is easy for code to silently drift out of sync with, and nothing would have caught it except the user noticing the dashboard's warning felt wrong

## Next Steps

- Open question, not yet answered: did jarkius.ai@gmail.com's Recent security activity page show anything (password change, security checkup, forced sign-out) in the Aug 25 05:01-Aug 26 22:36 window that could explain the second invalid_grant?
- Watch whether the newly-minted token (Aug 26 <home>23:00) survives more than <home>2 days — if it dies again quickly, the cause is very likely account-level, not the Testing-to-Production transition
- Open a PR for fix/gmail-oauth-published-flag -> main when ready (pushed but not yet merged, per the two-computer branch workflow rule)
- Consider generating a fresh Gmail app password if the SMTP fallback is still wanted as backup transport — current .env value is confirmed dead (535 BadCredentials) and OAuth is carrying 100% of sends right now
