"""Panel-process state: the last generate() result of THIS server process.

Kept separate from jobs.py so /preview can render without knowing job ids.
Preview byte-identity is same-run only by design (review m2): archives hold
markdown, not the rendered HTML nor the date_str the send used, so a fresh
server shows an explicit "no generation yet" state instead of a silently
re-dated reconstruction.
"""

LAST_GENERATION: dict | None = None  # generate()'s return dict, verbatim


def set_generation(result: dict) -> None:
    global LAST_GENERATION
    LAST_GENERATION = result


def get_generation() -> dict | None:
    return LAST_GENERATION
