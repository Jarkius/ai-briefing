"""S4 tests: research page, lock pre-reject, job reattach (AC7), and the
in-task lock acquisition (review m5).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from panel import jobs, state
from panel.app import _research_job, app

client = TestClient(app)


def teardown_function():
    jobs.JOBS.clear()


def test_research_page_lists_pending_newest_first(tmp_path):
    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("- [ ] older pending\n- [ ] newer pending\n")
    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)):
        r = client.get("/research")
    assert r.status_code == 200
    assert r.text.index("newer pending") < r.text.index("older pending")


def test_research_page_lists_completed_tasks_newest_first(research_db):
    from briefing import research_store

    conn = research_store.connect_at(research_db)
    older = research_store.insert_queued(conn, "older topic")
    research_store.mark_ready(conn, older, "older findings")
    newer = research_store.insert_queued(conn, "newer topic")
    research_store.mark_ready(conn, newer, "newer findings")

    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", "/nonexistent"):
        r = client.get("/research")
    assert r.text.index("newer topic") < r.text.index("older topic")


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


def test_research_run_rejects_long_prose_pasted_as_one_line(tmp_path):
    """A wall of AI-brainstormed prose with no line breaks and no URL would
    otherwise become ONE '- [ ] <giant paragraph>' line, get treated as a
    bare topic search query (garbage results), and still get checked off
    as done — silently losing the material with no trace. Reject it and
    point to /research/paste instead; nothing gets queued."""
    from briefing import researcher

    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("")
    long_prose = "x" * (researcher.TOPIC_LENGTH_GUARD_CHARS + 1)
    with patch("panel.app.mcp_client.is_locked", return_value=False), \
         patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)):
        r = client.post("/research/run", data={"text": long_prose})
    assert "Paste it directly" in r.text
    assert reqfile.read_text() == ""  # nothing queued
    assert not jobs.JOBS  # no job enqueued either


def test_research_run_allows_long_line_with_a_url(tmp_path):
    """The length guard only fires for prose with no link — a long topic
    description that legitimately contains a URL must still queue."""
    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("")
    from briefing import researcher

    long_with_url = "y" * (researcher.TOPIC_LENGTH_GUARD_CHARS + 1) + " https://example.com/x"
    with patch("panel.app.mcp_client.is_locked", return_value=False), \
         patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)), \
         patch("panel.app._research_job", new=AsyncMock(return_value="findings")):
        r = client.post("/research/run", data={"text": long_with_url})
    assert "https://example.com/x" in reqfile.read_text()
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


def test_research_findings_flow_into_next_regenerate(research_db):
    """Durability fix: findings persist to research_store (not just an
    in-process global), and _regenerate_job consumes them — recording
    which archive they landed in — ONLY after generate() succeeds, so a
    retried Regenerate after a crash can't silently skip real findings."""
    import asyncio

    from briefing import research_store
    from panel.app import _research_job

    async def fake_run_pending_async(phase_cb=None, on_result=None):
        on_result("some topic", "F1 findings")
        return "F1 findings", 1

    class FakeLock:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return None

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()), \
         patch("panel.app.researcher.run_pending_async", side_effect=fake_run_pending_async), \
         patch("panel.app._pathspec_commit", return_value=None):
        asyncio.run(_research_job(lambda t: None))

    conn = research_store.connect_at(research_db)
    ready_before = research_store.list_ready(conn)
    assert len(ready_before) == 1
    assert ready_before[0]["result_text"] == "F1 findings"

    # regenerate must consume them (and only once)
    from panel.app import _regenerate_job
    with patch("panel.app.db.connect") as conn_mock, \
         patch("panel.app.db.insert_run", return_value=1), \
         patch("panel.app.db.update_run"), \
         patch("panel.app.generator.generate", return_value={"ok": True, "archive_file": "briefing_x.md"}) as gen:
        conn_mock.return_value.close = lambda: None
        _regenerate_job()
    assert gen.call_args.kwargs["research_findings"] == "F1 findings"

    tasks = research_store.list_tasks(conn)
    assert tasks[0]["state"] == "consumed"
    assert tasks[0]["archive_file"] == "briefing_x.md"
    assert research_store.list_ready(conn) == []  # consumed, not repeated
    state.LAST_GENERATION = None


def test_research_page_links_completed_tasks_to_their_archives(tmp_path, research_db):
    """The archive link comes from research_store's own recorded
    archive_file (set by mark_consumed), not from string-matching the
    checked request text against a receipt heading — that old mechanism
    silently failed for anything that didn't exactly match, and for
    /research/paste entries, which never wrote a receipt at all."""
    from briefing import research_store

    arch = tmp_path / "archives"
    arch.mkdir()
    (arch / "briefing_2026-07-25_0600.md").write_text("# AI Briefing\n## S1\nx\n", encoding="utf-8")

    conn = research_store.connect_at(research_db)
    task_id = research_store.insert_queued(conn, "https://youtu.be/abc")
    research_store.mark_ready(conn, task_id, "the transcript findings")
    research_store.mark_consumed(conn, [task_id], "briefing_2026-07-25_0600.md")

    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", "/nonexistent"), \
         patch("panel.app.config.ARCHIVE_DIR", str(arch)):
        r = client.get("/research")
    assert "→ in" in r.text
    assert "/archive?view=briefing_2026-07-25_0600.md" in r.text
    assert "included" in r.text


def test_research_paste_files_material_for_next_regenerate(research_db):
    from briefing import research_store

    r = client.post("/research/paste", data={
        "title": "Keynote notes",
        "content": "Speaker said models will be free by 2030.",
    })
    assert "filed" in r.text

    conn = research_store.connect_at(research_db)
    ready = research_store.list_ready(conn)
    assert len(ready) == 1
    assert "Keynote notes" in ready[0]["result_text"]
    assert "rewrite into newsletter style" in ready[0]["result_text"]
    assert "free by 2030" in ready[0]["result_text"]


def test_research_paste_appends_not_clobbers(research_db):
    from briefing import research_store

    client.post("/research/paste", data={"title": "earlier", "content": "existing findings"})
    client.post("/research/paste", data={"title": "extra", "content": "more notes"})

    conn = research_store.connect_at(research_db)
    ready = research_store.list_ready(conn)
    combined = "\n".join(t["result_text"] for t in ready)
    assert "existing findings" in combined
    assert "more notes" in combined


def test_research_paste_rejects_empty():
    r = client.post("/research/paste", data={"title": "x", "content": "   "})
    assert "nothing pasted" in r.text


def test_research_job_appends_findings_never_clobbers_paste(research_db):
    # hunt-panel HIGH#3: paste while research runs -> job completion must
    # not discard the pasted block. Durability fix: both now live as
    # separate rows in research_store, so there's no shared-global append
    # order to race on at all.
    import asyncio

    from briefing import research_store
    from panel.app import _research_job

    class FakeLock:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return None

    client.post("/research/paste", data={"title": "pasted by user", "content": "my notes"})

    async def fake_run_pending_async(phase_cb=None, on_result=None):
        on_result("some topic", "### job findings\n\nfrom research")
        return "### job findings\n\nfrom research", 1

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()), \
         patch("panel.app.researcher.run_pending_async", side_effect=fake_run_pending_async), \
         patch("panel.app._pathspec_commit", return_value=None):
        asyncio.run(_research_job(lambda t: None))

    conn = research_store.connect_at(research_db)
    ready = research_store.list_ready(conn)
    combined = "\n".join(t["result_text"] for t in ready)
    assert "my notes" in combined
    assert "from research" in combined


# ---- search + detail view (mini research browser) ------------------------


def test_research_search_filters_completed_list(research_db):
    from briefing import research_store

    conn = research_store.connect_at(research_db)
    a = research_store.insert_queued(conn, "multi-agent orchestration")
    research_store.mark_ready(conn, a, "findings about agents")
    b = research_store.insert_queued(conn, "unrelated cooking topic")
    research_store.mark_ready(conn, b, "findings about recipes")

    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", "/nonexistent"):
        r = client.get("/research", params={"q": "agent"})
    assert "multi-agent orchestration" in r.text
    assert "unrelated cooking topic" not in r.text
    assert 'value="agent"' in r.text  # search box retains the query


def test_research_search_empty_query_shows_everything(research_db):
    from briefing import research_store

    conn = research_store.connect_at(research_db)
    research_store.insert_queued(conn, "topic one")
    research_store.insert_queued(conn, "topic two")

    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", "/nonexistent"):
        r = client.get("/research")
    assert "topic one" in r.text
    assert "topic two" in r.text


def test_research_detail_page_shows_full_findings(research_db):
    from briefing import research_store

    conn = research_store.connect_at(research_db)
    task_id = research_store.insert_queued(conn, "peer-to-peer agent councils")
    research_store.mark_ready(conn, task_id, "the full findings text, quite long")

    r = client.get(f"/research/{task_id}")
    assert r.status_code == 200
    assert "peer-to-peer agent councils" in r.text
    assert "the full findings text, quite long" in r.text
    assert "not yet included" in r.text


def test_research_detail_page_links_to_its_archive(tmp_path, research_db):
    from briefing import research_store

    arch = tmp_path / "archives"
    arch.mkdir()
    (arch / "briefing_2026-08-05_0900.md").write_text("# AI Briefing\n", encoding="utf-8")

    conn = research_store.connect_at(research_db)
    task_id = research_store.insert_queued(conn, "some topic")
    research_store.mark_ready(conn, task_id, "findings")
    research_store.mark_consumed(conn, [task_id], "briefing_2026-08-05_0900.md")

    with patch("panel.app.config.ARCHIVE_DIR", str(arch)):
        r = client.get(f"/research/{task_id}")
    assert "/archive?view=briefing_2026-08-05_0900.md" in r.text
    assert "✓ included" in r.text


def test_research_detail_page_404s_for_unknown_id(research_db):
    r = client.get("/research/999999")
    assert r.status_code == 404
    assert "No research task" in r.text
