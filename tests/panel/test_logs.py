"""S6 tests: logs page — cron tail, dashboard-jobs pane, phase strip."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from panel import jobs
from panel.app import app

client = TestClient(app)


def teardown_function():
    jobs.JOBS.clear()


def test_logs_page_loads_with_polling_shell():
    r = client.get("/logs")
    assert r.status_code == 200
    assert 'hx-get="/logs/tail"' in r.text


def test_logs_tail_shows_cron_log_and_keeps_polling(tmp_path):
    log = tmp_path / "briefing.log"
    log.write_text("line-one\nline-two\n")
    with patch("panel.app.config.LOG_PATH", str(log)):
        r = client.get("/logs/tail")
    assert "line-two" in r.text
    assert 'hx-trigger="every 3s"' in r.text


def test_logs_tail_truncates_to_last_200_lines(tmp_path):
    log = tmp_path / "briefing.log"
    log.write_text("\n".join(f"row-{i}" for i in range(300)) + "\n")
    with patch("panel.app.config.LOG_PATH", str(log)):
        r = client.get("/logs/tail")
    assert "row-299" in r.text
    assert "row-50" not in r.text


def test_logs_tail_shows_dashboard_jobs():
    j = jobs.Job(name="research", status="done")
    j.phase_text = "done"
    jobs.JOBS["abc"] = j
    with patch("panel.app.config.LOG_PATH", "/nonexistent"):
        r = client.get("/logs/tail")
    assert "research" in r.text
    assert "job-done" in r.text


def test_phase_strip_reads_latest_per_phase(tmp_path):
    import sqlite3

    from briefing import db as bdb

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    # cron run with collect ok; later dashboard-only send with send error —
    # the strip must show BOTH (per-phase latest, not single latest row).
    conn = fake_connect()
    r1 = bdb.insert_run(conn, source="cron", started_at="2026-07-25T05:00:00")
    bdb.update_run(conn, r1, collect_status="ok")
    r2 = bdb.insert_run(conn, source="dashboard", started_at="2026-07-25T09:00:00")
    bdb.update_run(conn, r2, send_status="error")
    conn.close()

    with patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.config.LOG_PATH", "/nonexistent"):
        r = client.get("/logs/tail")
    assert "collect: ok" in r.text
    assert "send: error" in r.text
    assert "(cron)" in r.text
    assert "(dashboard)" in r.text


def test_log_tail_highlights_errors_and_wins(tmp_path):
    log = tmp_path / "briefing.log"
    log.write_text(
        "[05:00] === Phase 1: Collect ===\n"
        "[05:01] Gemini direct call failed (attempt 1/4): HTTP 429\n"
        "[05:02] retrying in 10s\n"
        "sent via Gmail API (HTTPS/443)\n"
    )
    with patch("panel.app.config.LOG_PATH", str(log)):
        r = client.get("/logs/tail")
    assert 'class="ll-err"' in r.text   # the failed line
    assert 'class="ll-ok"' in r.text    # the sent line
    assert 'class="ll-warn"' in r.text  # the retry line


def test_log_highlight_escapes_html(tmp_path):
    log = tmp_path / "briefing.log"
    log.write_text("error: <script>alert(1)</script>\n")
    with patch("panel.app.config.LOG_PATH", str(log)):
        r = client.get("/logs/tail")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
