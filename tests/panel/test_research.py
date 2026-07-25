"""S4 tests: research page, lock pre-reject, job reattach (AC7), and the
in-task lock acquisition (review m5).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from panel import jobs
from panel.app import _research_job, app

client = TestClient(app)


def teardown_function():
    jobs.JOBS.clear()


def test_research_page_lists_requests_newest_first(tmp_path):
    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("- [x] old done one\n- [ ] newer pending one\n")
    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)):
        r = client.get("/research")
    assert r.status_code == 200
    assert r.text.index("newer pending one") < r.text.index("old done one")


def test_research_run_rejected_when_lock_held(tmp_path):
    with patch("panel.app.mcp_client.is_locked", return_value=True):
        r = client.post("/research/run", data={"text": "some topic"})
    assert "lock held" in r.text or "Collection is running" in r.text
    assert not jobs.JOBS  # nothing enqueued


def test_research_run_appends_pasted_lines_and_enqueues(tmp_path):
    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("- [ ] existing\n")
    with patch("panel.app.mcp_client.is_locked", return_value=False), \
         patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)), \
         patch("panel.app._research_job", new=AsyncMock(return_value="findings")):
        r = client.post("/research/run", data={"text": "topic A\nhttps://example.com/b"})
    content = reqfile.read_text()
    assert "- [ ] topic A" in content
    assert "- [ ] https://example.com/b" in content
    assert "hx-get=\"/jobs/" in r.text


def test_research_page_reattaches_running_job():
    # AC7: reload while a research job runs -> polling fragment re-rendered.
    jobs.JOBS["livejob12345"] = jobs.Job(name="research", phase_text="fetching page…")
    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", "/nonexistent"):
        r = client.get("/research")
    assert "hx-get=\"/jobs/livejob12345\"" in r.text
    assert "fetching page" in r.text


def test_research_job_acquires_lock_inside_task():
    # review m5: the lock context must be entered within the job coroutine.
    import asyncio

    entered = []

    class FakeLock:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *a):
            entered.append(False)

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()) as mock_lock, \
         patch("panel.app.researcher.run_pending_async", new=AsyncMock(return_value=("f", 0))):
        result = asyncio.run(_research_job(lambda t: None))

    mock_lock.assert_called_once_with(retry_seconds=0)
    assert entered == [True, False]
    assert "no unchecked requests" in result


def test_research_job_commits_only_when_requests_processed():
    import asyncio

    class FakeLock:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return None

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()), \
         patch("panel.app.researcher.run_pending_async", new=AsyncMock(return_value=("findings!", 2))), \
         patch("panel.app._pathspec_commit", return_value=None) as commit:
        result = asyncio.run(_research_job(lambda t: None))

    commit.assert_called_once()
    assert result == "findings!"
