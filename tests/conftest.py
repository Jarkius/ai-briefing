import pytest

from briefing import config


@pytest.fixture(autouse=True)
def _no_live_bitwarden_fetch(monkeypatch):
    """config._load_env() calls _load_bitwarden(), which shells out to the
    real bws CLI/network if data/bws_access_token exists on the machine
    running the tests. Point it at a path that never exists so the whole
    suite stays offline and fast regardless of which machine runs it."""
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", "/nonexistent/bws_access_token")
