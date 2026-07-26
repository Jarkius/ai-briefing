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


def test_research_done_fragment_shows_findings():
    j = jobs.Job(name="research", status="done")
    j.result = "### my request\n\nInteresting finding <b>escaped</b>"
    jobs.JOBS["donejob"] = j
    r = client.get("/jobs/donejob")
    assert "Interesting finding" in r.text
    assert "&lt;b&gt;escaped&lt;/b&gt;" in r.text  # findings are HTML-escaped
    assert "Requested Research" in r.text
    assert "every 2s" not in r.text  # terminal — no more polling


def test_research_findings_flow_into_next_regenerate():
    import asyncio

    from panel import state
    from panel.app import _research_job

    class FakeLock:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return None

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()), \
         patch("panel.app.researcher.run_pending_async", new=AsyncMock(return_value=("F1 findings", 1))), \
         patch("panel.app._pathspec_commit", return_value=None):
        asyncio.run(_research_job(lambda t: None))

    # regenerate must consume them (and only once)
    from panel.app import _regenerate_job
    with patch("panel.app.db.connect") as conn_mock, \
         patch("panel.app.db.insert_run", return_value=1), \
         patch("panel.app.db.update_run"), \
         patch("panel.app.generator.generate", return_value={"ok": True}) as gen:
        conn_mock.return_value.close = lambda: None
        _regenerate_job()
    assert gen.call_args.kwargs["research_findings"] == "F1 findings"
    assert state.LAST_RESEARCH_FINDINGS == ""  # consumed, not repeated
    state.LAST_GENERATION = None


def test_research_page_links_requests_to_their_archives(tmp_path):
    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("- [x] https://youtu.be/abc (researched 2026-07-25)\n")
    arch = tmp_path / "archives"
    arch.mkdir()
    (arch / "briefing_2026-07-25_0600.md").write_text(
        "# AI Briefing\n## S1\nx\n\n## 🔍 Requested Research (included in this issue)\n- https://youtu.be/abc\n"
    )
    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)), \
         patch("panel.app.config.ARCHIVE_DIR", str(arch)):
        r = client.get("/research")
    assert "→ in" in r.text
    assert "/archive?view=briefing_2026-07-25_0600.md" in r.text


def test_research_paste_files_material_for_next_regenerate():
    from panel import state

    state.LAST_RESEARCH_FINDINGS = ""
    r = client.post("/research/paste", data={
        "title": "Keynote notes",
        "content": "Speaker said models will be free by 2030.",
    })
    assert "filed" in r.text
    assert "Keynote notes" in state.LAST_RESEARCH_FINDINGS
    assert "rewrite into newsletter style" in state.LAST_RESEARCH_FINDINGS
    assert "free by 2030" in state.LAST_RESEARCH_FINDINGS
    state.LAST_RESEARCH_FINDINGS = ""


def test_research_paste_appends_not_clobbers():
    from panel import state

    state.LAST_RESEARCH_FINDINGS = "### earlier research\n\nexisting findings"
    client.post("/research/paste", data={"title": "extra", "content": "more notes"})
    assert "existing findings" in state.LAST_RESEARCH_FINDINGS
    assert "more notes" in state.LAST_RESEARCH_FINDINGS
    state.LAST_RESEARCH_FINDINGS = ""


def test_research_paste_rejects_empty():
    r = client.post("/research/paste", data={"title": "x", "content": "   "})
    assert "nothing pasted" in r.text
