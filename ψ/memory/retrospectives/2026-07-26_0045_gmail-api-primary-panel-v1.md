---
author: claude-fable-5
machine: Chakkrits-MacBook-Air
session: 0d020eff (continued from 1c844dd0)
date: 2026-07-26
project: ai-briefing
privacy: repo-safe
---

# Retrospective: Gmail API primary + acceptance closure + control panel v1

📡 Session: 0d020eff | ai-briefing | ~5.5h (19:00 → 00:45 +07)
**Type**: Feature marathon (transport hardening → acceptance → reviews → panel build)
**Branches**: `feat/mcp-collector` (closed out, PR #2 ready) → `feat/control-panel` (v1 built)

## Session Summary

Longest and densest session on this project. Five distinct arcs:

1. **Scheduled-run proof + Gmail API primary**: two 3-minute-out launchd tests both delivered. Mid-arc the user made the call to switch sender identity to a dedicated `jarkius.ai@gmail.com` and asked "rearrange to use 443 so we can be sure it always works" — led to OAuth setup (user clicked consent after a test-user 403), a real in-anger API send test, and promoting Gmail API to PRIMARY transport for both send and the dedup pre-check (SMTP/IMAP demoted to fallback). Multi-recipient support added (jsanitareephon@deloitte.com), verified by a third live scheduled run at 20:13.
2. **Acceptance closure via /plan**: AC7 (style marker — found and fixed a bug in the harness itself), AC5 (real Wi-Fi-kill offline test, exit 0), AC8 (wall-clock evidence), feed hygiene (errors 8→1; 4 URLs fixed, 3 WAF/dead feeds retired to `subscriptions_retired.json`), QA gate (caught Windows `%-d` strftime crash that silently killed every Windows Generate), PR #2 updated with evidence.
3. **Three-lane /code-review** (security, correctness, panel-readiness) before the panel build: 2 HIGH security (unignored `.env.bak` with live creds; PS injection via recipients), token-refresh race, SSRF guard, Windows lock dead code (found independently by TWO lanes — the prior "fix" had built the msvcrt shim but never wired it), `config.reload()`, archive collision. All fixed, all tested.
4. **Control panel v1** via /plan + bmad-agentic-flow: 6 stories (S1–S6), each committed green. FastAPI+htmx, jobs registry with to_thread dispatch, preview with byte-identity iframes, live research with in-task locking, 4 form pages, logs with per-phase strip. Then two user-driven additions: archive browser (left date list / right rendered newsletter, rendering with the ARCHIVE's own date) and research-findings display + flow into regenerate (which was silently dropped before — panel always passed empty findings).
5. **AC6 live proof**: 3.5-hour Karpathy video pasted into the panel → transcribed ~10min → findings shown → checkbox flipped + pathspec commit → regenerate folded "Requested Research" into the newsletter (surviving a Gemini 429 via the fallback chain).

Also: git push broke mid-session — gh CLI had switched active account to `jarkius-ai` (no repo access) while the keychain still held the working `Jarkius` token. Root-caused the credential-helper chain, pinned repo-local `credential.helper osxkeychain`.

## Timeline

**Date**: 2026-07-25 → 26 (GMT+7)
*Provenance: dig-miner's .jsonl extraction only reached 18:50 (tonight's 19:00→00:45 messages weren't in the captured files) — rows below are conversation memory anchored to log-verified clock times (briefing.log launchd fires at 19:05/20:13, OAuth file mtimes 19:33/19:47, git commit times). Miner did confirm project arc: DB-redesign discussion 07-22, "gogogo" 07-23 00:00, retry-resume pattern 07-24/25.*

| Time | What |
|---|---|
| 19:00 | /recap — orientation; 3 new commits since TODO (Gmail API fallback code, dup-send fix) |
| 19:01 | User: "set the next 3 minute schedule for test" → 19:05 launchd run: sent via SMTP |
| ~19:15 | User: "rearrange to use 443 so we can surely it always work" — discovered OAuth never done |
| 19:25 | User switched sender to jarkius.ai@gmail.com ("for life safe") — .env edit, backup |
| 19:33–19:47 | OAuth: client secret (double .json.json fixed), 403 test-user gate, consent OK |
| 19:48 | First real Gmail-API send delivered |
| ~20:00 | sender.py reordered API-first; require_env relaxed; 78 tests; commit 332b145 |
| 20:09 | User: add jsanitareephon@deloitte.com + full test → RECIPIENT_EMAILS list plumbed |
| 20:13 | Scheduled run: both parts "sent via Gmail API (HTTPS/443)" — API-first path proven |
| 20:19–20:40 | /plan → AC7 pass (harness sed bug fixed), AC5 Wi-Fi-kill pass, feed hygiene 8→1 |
| ~20:50 | QA gate: Windows %-d strftime MAJOR found+fixed; PR #2 updated with evidence |
| 22:00 | (new session) /code-review high relaunched after process exit — 3 lanes spawned |
| 22:10–22:50 | Security 2H/2M fixed; panel-readiness 4 findings fixed; correctness 2 new fixed |
| 22:55 | Push 403 — gh active account jarkius-ai; keychain had Jarkius; repo-local helper pin |
| 23:05 | User: "new branch + /plan as /bmad-agentic-flow" → stories doc + readiness gate |
| 23:10–23:59 | S1–S6 built, each committed green; live-verified each story on :8787 |
| 00:05 | User: "site can't load" — server was my stopped smoke instance; started properly |
| 00:10 | User: archive browser request → built (renders with archive's own date) |
| 00:20 | User: "where will research show up?" → findings display + regenerate flow fixed |
| 00:25–00:40 | AC6 live: Karpathy 3.5h video → transcript → newsletter section (via 429 fallback) |
| 00:45 | /rrr |

## Files Modified (highlights)

- `src/briefing/sender.py` — API-first send+pre-check, multi-recipient, PS recipient escaping
- `src/briefing/gmail_api.py` — primary-transport docstring, atomic 0600 token refresh
- `src/briefing/config.py` — RECIPIENT_EMAILS, _bind()/reload(), recipient guard
- `src/briefing/mcp_client.py` — lock paths through platform helpers (Windows fix)
- `src/briefing/researcher.py` — SSRF guard (_public_url_error)
- `src/briefing/collector.py` — nameless-subscription loud skip
- `src/briefing/generator.py` — %-d fix, archive seconds
- `src/panel/*` — entire panel: app.py, jobs.py, state.py, 9 templates, panel.css
- `tests/` — 54 → 148 tests across the session
- `subscriptions.json` + `subscriptions_retired.json`, `.gitignore`, `TODO.md`, README, panel.sh

## AI Diary

This session had a rhythm I want to remember: user instinct → my verification → real fix. "Rearrange to use 443 so we can be sure" was the user seeing what I'd glossed — I had reported the Gmail API fallback as shipped (commit existed!) without checking that `is_configured()` could ever return True on this machine. The token didn't exist. The fallback was decorative. That correction shaped the whole evening.

The review phase humbled me twice. Two independent review lanes flagged the Windows lock as dead code — a "fix" from a prior QA gate that built the msvcrt shim and then never called it. I had described that fix as complete in TODO.md. And the `.env.bak` I myself created at 19:25 became the security review's top HIGH finding three hours later: I created a live-credential file that wasn't gitignore-covered, in a repo where `git add -A` is a habit.

[→ AGENT DECISION] The panel dropped research findings on the floor — my S3 `_regenerate_job` hardcoded `research_findings=""` and I shipped S4's research route without ever tracing where its output went. The user caught it with one question ("where will the Research show up?"). I had built both ends of a pipe and never connected them, because each story verified green in isolation. Story-level tests passed; the cross-story data flow had no test and no reviewer.

The panel build itself was the smoothest multi-hour stretch I've had on this project — the story discipline (drift-reconciliation first, review patterns named up front, one story committed green at a time) meant zero backtracking. The BMAD readiness gate earned its ceremony.

## Honest Feedback

1. **Smoke-test servers caused user-facing confusion.** I started and killed uvicorn repeatedly for verification; the user then hit "site can't load" on a dead URL I'd shown them. I should have left a server running the moment a human was going to click, or said explicitly "not running yet."
2. **The 10-minute poll loop timed out and burned wall-clock.** I foreground-polled a 3.5-hour-video transcription with a 10-min timeout, hit the ceiling, then had to re-attach in background. Long jobs should go straight to `run_in_background` polling.
3. **gh auth drift ate ~15 minutes mid-flow.** The jarkius.ai account creation (earlier, outside this session) had flipped gh's active account; pushes 403'd at the worst moment (mid-review-fixes). Not my doing, but I retried the same push three times before root-causing the credential-helper chain.

## Lessons Learned

- **"Shipped" for a fallback/transport means a live in-anger test, not code-exists.** A fallback whose config file was never created is decorative. Verify `is_configured()`-style gates return True on the actual machine before reporting capability.
- **When two ends of a pipe are built in separate stories, add a cross-story test for the flow between them.** Per-story green tests prove parts, not plumbing. (Panel: research → regenerate handoff had no test until the user asked where the output went.)
- **Any file you create containing credentials must be gitignore-verified in the same breath** (`git check-ignore <file>` immediately). My own backup became the top security finding.
- **A "fixed" platform-specific bug needs a regression test that greps the actual call sites.** The Windows lock shim existed for weeks while the code called fcntl directly — a 5-line AST/grep test would have caught it at fix time.
- **Repo-local `credential.helper` pin beats fighting global gh auth state** when one repo needs a specific identity (two-account GitHub setups).

## Next Steps

- [ ] 5am scheduled run — first real morning proof of API-first path (check log: "sent via Gmail API")
- [ ] User merges PR #2 (feat/mcp-collector → main), then PR for feat/control-panel
- [ ] Style question left open: per-section rules in newsletter_style.md (works today) vs structural per-section style files — user deciding
- [ ] Manual ACs: AC2 LAN curl, AC7/AC8 kill-tab/kill-server, AC5 IMAP count
- [ ] Windows machine: own OAuth token + verify Windows lock fix on real win32
- [ ] Gemini quota exhausted until 14:00 +07 — today's runs lean on Claude CLI fallback

## 🔍 Self-Audit
- shipped: 19 commits across two branches — Gmail-API-primary (332b145), multi-recipient (c506017), acceptance closure (69929ad), QA fixes (a03c8b3), security fixes (e56461b), panel-readiness fixes (e4ffa68), correctness fixes (983e5cb), panel S1–S6 (5f30674…9914b8e), archive browser (be8c6a9), research flow (707ebf7), stale-job msg (5a461ef)
- blocked: PR merges (user's call); AC2/AC7/AC8 manual tests (need user/second machine); Windows verification (needs that machine); Gemini quota until 14:00
- uncomfortable truth: [→ AGENT DECISION] I built the research route and the regenerate route in separate stories and never connected them — regenerate hardcoded empty findings; the user found it by asking one question
- friction: 3 points (operational: smoke-server confusion, poll-timeout on long transcription, gh auth drift | strategic: none)
- next steps: 6 — each names its actor and evidence source
- rationalizations caught: 1 — I initially framed the research-flow gap as "the plan intended display later" before admitting the panel actively passed "" and the data went nowhere
