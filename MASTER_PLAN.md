# MASTER_PLAN — ai-briefing: morning AI-awareness feed, complete & bug-free

**Planner:** Claude Fable 5 · **Date:** 2026-07-23 · **Branch:** `feat/mcp-collector` → PR to `main`
**Goal:** The pipeline reliably delivers the AI-awareness briefing every morning at 06:00 via launchd, with test coverage, passing acceptance criteria, and a security-reviewed merge.

## Current state (audited, evidence-based)

- MCP collector pipeline implemented and committed (`4bd6aca`), merged with `origin/main` (`68edb81`), link-fix + SMTP-retry committed (`c6e1d1c`). Pushed.
- **Defect D1 (critical):** launchd still runs the LEGACY pipeline — plist edited but never reloaded. Evidence: `briefing.log` 2026-07-23 06:53 shows old collectors + DNS failures.
- **Defect D2:** Windows wrapper `run_briefing.bat:24` invokes legacy `ai_briefing.py` (merged from other machine via PR #1). README notes this; scripts not yet updated.
- **Gap G1:** no `tests/` — plan verification step 1 unmet.
- **Gap G2:** acceptance criteria AC5 (offline soft-fail), AC7 (style-marker), AC8 (wall-clock) unverified.
- Repo is PUBLIC — security review must confirm no credentials/PII in tracked files before merge.

## Team topology (tmux session `ai-briefing-team`)

| Pane | Role | Model | Scope |
|---|---|---|---|
| 0 | Principal / Planner | Fable 5 (this session) | Orchestration, task dispatch, merge authority |
| 1 | Dev-A | Sonnet | T2: test suite (`tests/`) |
| 2 | Dev-B | Sonnet | T3: Windows wrapper update + AC7 style-marker harness |
| 3 | QA | Fable 5 | T5: run suites, edge cases, security review — gate before merge |

Rules of engagement:
- Developers work ONLY in their assigned files; commit to `feat/mcp-collector` with clear messages; never push force; never touch `.env`, `data/`, `main`.
- No developer merge without QA pane sign-off (tests green + security review clean).
- QA has read access to everything; write access only to `tests/` fixes and `QA_REPORT.md`.

## Task breakdown & constraints

### T1 — launchd acceptance (Principal, this machine, sequential-first)
1. `cp com.user.ai-briefing.plist ~/Library/LaunchAgents/` (or confirm current install path)
2. `launchctl unload` + `load` the LaunchAgent
3. `launchctl start com.user.ai-briefing`; verify `briefing.log` shows NEW pipeline phases (collect/research/generate/send) and exit 0
4. Acceptance: log lines from the launchd-triggered run, not a terminal run
Constraint: do not change schedule time (06:00). Soft-fail behavior must hold (a network error must not abort the run).

### T2 — Test suite (Dev-A)
Files: `tests/` (new), `pyproject.toml` (add pytest to dev extras only).
- `test_researcher.py`: parse `- [ ]`/`- [x]` lines; YouTube vs URL vs topic routing decision (pure-function level, no MCP calls — mock session)
- `test_generator.py`: prompt assembly respects 60k-char budget, 8k transcript cap, whole-item drop priority; `[title](url)` link format survives `_sanitize()`
- `test_db.py`: schema guard passes on fixture DB with expected columns, aborts on missing column; `sent_items` dedup query
- `test_sender.py`: two-part split; IMAP pre-check short-circuit (mock imaplib); `_with_retry` backoff behavior
Constraints: no network in tests; fixture SQLite built in tmp; runtime < 30s; do not modify `src/` — if a function is untestable, report back, don't refactor unilaterally.

### T3 — Windows wrapper + AC7 harness (Dev-B)
Files: `run_briefing.bat`, `fix_task_settings.ps1` (only if needed), `scripts/check_style_marker.sh` (new).
- Update `run_briefing.bat` to call `.venv` python + `run.py` (mirror macOS setup; document venv bootstrap for Windows in README Windows section)
- AC7 harness: script appends STYLE-MARKER rule to `newsletter_style.md`, runs `run.py --dry-run`, asserts marker in output, restores file (trap-safe)
Constraints: PowerShell/bat changes must be inert on macOS; style file must be byte-identical after harness runs.

### T4 — Acceptance runs AC5/AC8 (Principal + QA, needs live environment)
- AC5: run with network blocked mid-collect → exit 0, email built from existing DB
- AC8: time full run; assert < 5 min without new videos
Constraint: use `--dry-run` where a real send would double-deliver; real-send test only once, verified via IMAP.

### T5 — QA gate (QA pane, blocks merge)
- Run `pytest` fresh; run AC7 harness; review diff `main...feat/mcp-collector` for: secrets/PII (public repo), injection into Gemini prompt from untrusted feed content, SMTP/IMAP credential handling, subprocess/command construction in mcp_client, lock-file races
- Output: `QA_REPORT.md` — pass/fail per item, severity-rated findings
Merge criterion: zero critical/high findings, tests green, T1 evidence attached.

### T6 — Merge (Principal)
- PR `feat/mcp-collector` → `main` with QA report linked; merge after QA sign-off; verify next-morning run (or `launchctl start`) post-merge.

## Sequencing

```
T1 (Principal, now) ──┐
T2 (Dev-A) ───────────┼── T5 (QA gate) ── T6 (PR + merge) ── next-morning verification
T3 (Dev-B) ───────────┘
T4 (after T1, parallel with T5 prep)
```

## Definition of done

1. launchd-triggered run logs the new pipeline end-to-end, exit 0 (D1 closed)
2. `pytest` green, < 30s, no network (G1 closed)
3. AC5/AC7/AC8 evidenced in QA_REPORT.md (G2 closed)
4. Windows wrapper targets `run.py` (D2 closed)
5. QA security review: no critical/high findings on public-repo diff
6. PR merged to `main`; TODO.md updated; morning email arrives
