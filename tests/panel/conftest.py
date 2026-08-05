"""Panel test isolation: panel.state holds module globals mutated by many
tests — without a reset, findings text leaks across test MODULES (observed:
full-suite run failed a test that passed in isolation)."""

import pytest

from panel import jobs, state


@pytest.fixture(autouse=True)
def _reset_panel_state():
    state.LAST_GENERATION = None
    jobs.JOBS.clear()
    yield
    state.LAST_GENERATION = None
    jobs.JOBS.clear()


@pytest.fixture
def research_db(tmp_path):
    """Isolated research_tasks.db for tests exercising research_store
    durability through the panel routes — same tmp_path isolation posture
    as db.connect()'s own test fixtures."""
    from unittest.mock import patch

    from briefing import research_store

    db_path = str(tmp_path / "research_tasks.db")
    with patch("panel.app.research_store.connect", lambda: research_store.connect_at(db_path)):
        yield db_path
