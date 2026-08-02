---
author: claude-fable-5
machine: Chakkrits-MacBook-Air
session: 66f22bfa
date: 2026-07-23
project: ai-briefing
privacy: repo-safe
---

# Editing a launchd plist does not change what launchd runs

**Pattern**: The MCP-integration work rewrote `com.user.ai-briefing.plist` (new ProgramArguments → `.venv/bin/python run.py`, HOME env var), and every code phase passed inspection — yet the 2026-07-23 06:53 scheduled run still executed the *legacy* pipeline. launchd holds the job definition it loaded, not the file on disk; without `launchctl unload`/`load` (or `bootout`/`bootstrap`), the edit is inert.

**Why it matters**: This is the class of bug where "code-complete" silently diverges from "done." Everything greps correct; only the log from the real trigger tells the truth. The plan anticipated this: its acceptance criterion required a `launchctl start`-triggered run, not a terminal run.

**Rule of thumb**: For any scheduler-driven change (launchd, cron, systemd), acceptance evidence = a log line produced by the scheduler's own trigger after reloading the unit. Terminal runs prove nothing about the scheduled path.
