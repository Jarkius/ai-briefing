"""Panel-process state: the last generate() result of THIS server process.

Kept separate from jobs.py so /preview can render without knowing job ids.
Preview byte-identity is same-run only by design (review m2): archives hold
markdown, not the rendered HTML nor the date_str the send used, so a fresh
server shows an explicit "no generation yet" state instead of a silently
re-dated reconstruction.
"""

import threading

LAST_GENERATION: dict | None = None  # generate()'s return dict, verbatim

# Findings text from the last completed dashboard research job. The next
# dashboard Regenerate folds this into generate(research_findings=...) —
# same as run.py's research→generate handoff — then clears it so a later
# regenerate doesn't repeat a stale "Requested Research" section.
LAST_RESEARCH_FINDINGS: str = ""

# Mutations come from both the event loop and to_thread worker threads —
# real OS threads, so read-modify-write on the globals needs a real lock
# (a double-clicked Regenerate could otherwise split pop into two reads).
_LOCK = threading.Lock()


def set_generation(result: dict) -> None:
    global LAST_GENERATION
    with _LOCK:
        LAST_GENERATION = result


def get_generation() -> dict | None:
    return LAST_GENERATION


def add_research_findings(text: str) -> None:
    """Append a findings block — used by BOTH the paste form and completed
    research jobs. Append-only by design: a research job finishing must not
    clobber material the user pasted while it ran (and vice versa)."""
    global LAST_RESEARCH_FINDINGS
    with _LOCK:
        LAST_RESEARCH_FINDINGS = (
            f"{LAST_RESEARCH_FINDINGS}\n\n{text}" if LAST_RESEARCH_FINDINGS else text
        )


def pop_research_findings() -> str:
    global LAST_RESEARCH_FINDINGS
    with _LOCK:
        text, LAST_RESEARCH_FINDINGS = LAST_RESEARCH_FINDINGS, ""
    return text
