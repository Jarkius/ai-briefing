"""Panel-process state: the last generate() result of THIS server process.

Kept separate from jobs.py so /preview can render without knowing job ids.
Preview byte-identity is same-run only by design (review m2): archives hold
markdown, not the rendered HTML nor the date_str the send used, so a fresh
server shows an explicit "no generation yet" state instead of a silently
re-dated reconstruction.
"""

import threading

LAST_GENERATION: dict | None = None  # generate()'s return dict, verbatim

# {'post_text': str, 'date_str': str} from the last completed social-post
# job — separate from LAST_GENERATION since it's built from deep-fetched
# sources, not the daily digest's markdown, and has its own send button.
LAST_SOCIAL_POST: dict | None = None

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


def set_social_post(result: dict) -> None:
    global LAST_SOCIAL_POST
    with _LOCK:
        LAST_SOCIAL_POST = result


def get_social_post() -> dict | None:
    return LAST_SOCIAL_POST
