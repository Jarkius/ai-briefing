"""S3 tests: preview rendering, regenerate/send job flow, and AC3 — the
string-identity guarantee that /preview shows the exact in-memory HTML
sender would email (no second render path).
"""

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import jobs, state
from panel.app import app

client = TestClient(app)

FAKE_GEN = {
    "markdown": "# md",
    "part1_html": "<html><body>PART1-EXACT-BYTES &amp; more</body></html>",
    "part2_html": "<html><body>PART2-EXACT-BYTES</body></html>",
    "date_str": "Saturday, July 25, 2026",
    "today": "2026-07-25",
}


def teardown_function():
    state.LAST_GENERATION = None
    state.LAST_SOCIAL_POST = None
    jobs.JOBS.clear()


def test_preview_empty_state_before_any_generation():
    r = client.get("/preview")
    assert r.status_code == 200
    assert "The presses are quiet" in r.text
    assert "srcdoc" not in r.text


def test_preview_renders_both_parts_string_identical():
    # AC3: the srcdoc attribute must contain exactly the in-memory HTML,
    # HTML-attribute-escaped by Jinja (that escaping is reversible and is
    # what guarantees byte-identity of what the iframe renders).
    import html as html_lib
    import re

    state.set_generation(FAKE_GEN)
    r = client.get("/preview")
    srcdocs = re.findall(r'srcdoc="([^"]*)"', r.text)
    assert len(srcdocs) == 2
    assert html_lib.unescape(srcdocs[0]) == FAKE_GEN["part1_html"]
    assert html_lib.unescape(srcdocs[1]) == FAKE_GEN["part2_html"]
    assert FAKE_GEN["date_str"] in r.text


def test_send_without_generation_returns_error_banner_no_job():
    r = client.post("/preview/send")
    assert "regenerate first" in r.text
    assert not jobs.JOBS  # no job enqueued


def test_regenerate_enqueues_job_and_polling_fragment():
    with patch("panel.app._regenerate_job", return_value=FAKE_GEN):
        r = client.post("/preview/regenerate")
    assert "hx-get=\"/jobs/" in r.text or "banner" in r.text


def test_job_fragment_terminal_states_stop_polling():
    jobs.JOBS["jjj"] = jobs.Job(name="regenerate")
    r = client.get("/jobs/jjj")
    assert 'hx-trigger="every 2s"' in r.text  # running -> keeps polling

    jobs.JOBS["jjj"].status = "error"
    jobs.JOBS["jjj"].phase_text = "error: boom"
    r = client.get("/jobs/jjj")
    assert "every 2s" not in r.text  # terminal -> polling attribute gone
    assert "boom" in r.text


def test_send_result_already_sent_renders_distinct_banner():
    j = jobs.Job(name="send", status="done")
    j.result = {"part1": "already_sent", "part2": "already_sent"}
    jobs.JOBS["sss"] = j
    r = client.get("/jobs/sss")
    assert "already sent today" in r.text
    assert "banner-warn" in r.text


def test_send_result_sent_renders_ok_banner():
    j = jobs.Job(name="send", status="done")
    j.result = {"part1": "sent", "part2": "sent"}
    jobs.JOBS["sss"] = j
    r = client.get("/jobs/sss")
    assert "banner-ok" in r.text


def test_regenerate_job_writes_dashboard_runs_row(tmp_path, research_db):
    # _regenerate_job must insert a source='dashboard' runs row with
    # generate_status ok, and stash the result for /preview.
    import sqlite3

    from briefing import db as bdb

    db_path = str(tmp_path / "feeds.db")
    real_connect = bdb.connect

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    from panel.app import _regenerate_job

    with patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.generator.generate", return_value=FAKE_GEN):
        result = _regenerate_job()

    assert result == FAKE_GEN
    assert state.get_generation() == FAKE_GEN
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT source, generate_status FROM runs").fetchone()
    assert row["source"] == "dashboard"
    assert row["generate_status"] == "ok"


def test_preview_shows_pending_strip(tmp_path, research_db):
    from briefing import research_store

    reqfile = tmp_path / "research_requests.md"
    reqfile.write_text("- [ ] pending topic one\n- [x] done already\n")
    conn = research_store.connect_at(research_db)
    task_id = research_store.insert_queued(conn, "something")
    research_store.mark_ready(conn, task_id, "findings here")

    with patch("panel.app.config.RESEARCH_REQUESTS_PATH", str(reqfile)), \
         patch("panel.app._list_archives", return_value=[
             {"send_status": ""}, {"send_status": "sent"}, {"send_status": ""}]):
        r = client.get("/preview")
    assert "1 research request unprocessed" in r.text
    assert "research findings ready" in r.text
    assert "2 drafts in the archive" in r.text


def test_regenerate_double_submit_reattaches_not_duplicates():
    jobs.JOBS.clear()
    jobs.JOBS["busy1"] = jobs.Job(name="regenerate")  # running
    r = client.post("/preview/regenerate")
    assert "busy1" in r.text        # reattached to the running job
    assert len(jobs.JOBS) == 1      # no second job spawned
    jobs.JOBS.clear()


def test_send_double_submit_reattaches():
    state.set_generation(FAKE_GEN)
    jobs.JOBS["sendbusy"] = jobs.Job(name="send")
    try:
        r = client.post("/preview/send")
        assert "sendbusy" in r.text
        assert len(jobs.JOBS) == 1
    finally:
        jobs.JOBS.clear()
        state.LAST_GENERATION = None


# ---- social post cart ---------------------------------------------------------


def test_preview_shows_section_checkboxes():
    r = client.get("/preview")
    assert "Social Post Focus" in r.text
    assert 'name="sections" value="0"' in r.text
    assert 'name="sections" value="5"' in r.text


def test_preview_no_social_post_pane_before_any_build():
    r = client.get("/preview")
    assert "Share-Ready Post" not in r.text


FAKE_SOCIAL_POST = {"post_text": "🚀 GENERATED POST BODY\n🔹 point one", "date_str": "Thursday, July 30, 2026"}


def test_preview_shows_social_post_after_build():
    state.set_social_post(FAKE_SOCIAL_POST)
    r = client.get("/preview")
    assert "Share-Ready Post" in r.text
    assert "GENERATED POST BODY" in r.text
    assert FAKE_SOCIAL_POST["date_str"] in r.text


def test_social_post_build_enqueues_job():
    with patch("panel.app._social_post_job", return_value=FAKE_SOCIAL_POST) as mock_job:
        r = client.post("/preview/social-post", data={"sections": ["0", "2"]})
    assert 'hx-get="/jobs/' in r.text
    mock_job.assert_called_once_with([0, 2])
    jobs.JOBS.clear()


def test_social_post_build_with_no_sections_selected():
    with patch("panel.app._social_post_job", return_value=FAKE_SOCIAL_POST) as mock_job:
        client.post("/preview/social-post", data={})
    mock_job.assert_called_once_with([])
    jobs.JOBS.clear()


def test_social_post_build_double_submit_reattaches():
    jobs.JOBS.clear()
    jobs.JOBS["spbusy"] = jobs.Job(name="social-post")
    r = client.post("/preview/social-post", data={})
    assert "spbusy" in r.text
    assert len(jobs.JOBS) == 1
    jobs.JOBS.clear()


def test_social_post_job_fragment_success_message():
    jobs.JOBS["spdone"] = jobs.Job(name="social-post", status="done", result=FAKE_SOCIAL_POST)
    r = client.get("/jobs/spdone")
    assert "social post ready" in r.text
    jobs.JOBS.clear()


def test_social_post_job_fragment_error_message():
    jobs.JOBS["sperr"] = jobs.Job(
        name="social-post", status="error",
        phase_text="error: no fetchable sources in this selection (no URLs, or all fetches failed) — try a different section",
    )
    r = client.get("/jobs/sperr")
    assert "social-post failed" in r.text
    assert "no fetchable sources" in r.text
    jobs.JOBS.clear()


def test_social_post_send_without_generation_returns_error_banner_no_job():
    r = client.post("/preview/social-post/send")
    assert "build a social post first" in r.text
    assert not jobs.JOBS


def test_social_post_send_enqueues_job():
    state.set_social_post(FAKE_SOCIAL_POST)
    with patch("panel.app._social_post_send_job", return_value="sent"):
        r = client.post("/preview/social-post/send")
    assert 'hx-get="/jobs/' in r.text
    jobs.JOBS.clear()


def test_social_post_send_double_submit_reattaches():
    state.set_social_post(FAKE_SOCIAL_POST)
    jobs.JOBS["spsendbusy"] = jobs.Job(name="social-post-send")
    r = client.post("/preview/social-post/send")
    assert "spsendbusy" in r.text
    assert len(jobs.JOBS) == 1
    jobs.JOBS.clear()


def test_social_post_send_job_fragment_sent():
    jobs.JOBS["spsent"] = jobs.Job(name="social-post-send", status="done", result="sent")
    r = client.get("/jobs/spsent")
    assert "social post sent" in r.text
    assert "banner-ok" in r.text
    jobs.JOBS.clear()


def test_social_post_send_job_fragment_already_sent():
    jobs.JOBS["spalready"] = jobs.Job(name="social-post-send", status="done", result="already_sent")
    r = client.get("/jobs/spalready")
    assert "already sent today" in r.text
    assert "banner-warn" in r.text
    jobs.JOBS.clear()


def test_social_post_send_job_fragment_error():
    jobs.JOBS["spfail"] = jobs.Job(name="social-post-send", status="done", result="error: smtp down")
    r = client.get("/jobs/spfail")
    assert "social post send failed" in r.text
    assert "banner-err" in r.text
    jobs.JOBS.clear()


def test_social_post_job_raises_when_no_fetchable_sources(tmp_path):
    import sqlite3

    from briefing import db as bdb
    from panel.app import _social_post_job

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    with patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.generator.social_post_candidate_items", return_value=[{"title": "x", "url": ""}]), \
         patch("panel.app.researcher.deep_fetch_items_sync", return_value=[]):
        try:
            _social_post_job([])
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no fetchable sources" in str(e)


def test_social_post_job_passes_section_indices_through(tmp_path):
    import sqlite3

    from briefing import db as bdb
    from panel.app import _social_post_job

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    with patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.generator.social_post_candidate_items", return_value=[{"title": "x", "url": "https://example.com"}]) as mock_candidates, \
         patch("panel.app.researcher.deep_fetch_items_sync", return_value=[{"title": "x", "url": "https://example.com", "content": "body"}]), \
         patch("panel.app.generator.build_social_post_source", return_value="material"), \
         patch("panel.app.generator.generate_social_post", return_value="generated post"):
        result = _social_post_job([1, 3])

    mock_candidates.assert_called_once()
    _, kwargs = mock_candidates.call_args
    assert kwargs["section_indices"] == [1, 3]
    assert result["post_text"] == "generated post"
    assert state.get_social_post() == result
