"""Panel test isolation: panel.state holds module globals mutated by many
tests — without a reset, findings text leaks across test MODULES (observed:
full-suite run failed a test that passed in isolation)."""

import pytest

from panel import jobs, state


@pytest.fixture(autouse=True)
def _reset_panel_state():
    state.LAST_GENERATION = None
    state.LAST_RESEARCH_FINDINGS = ""
    jobs.JOBS.clear()
    yield
    state.LAST_GENERATION = None
    state.LAST_RESEARCH_FINDINGS = ""
    jobs.JOBS.clear()
