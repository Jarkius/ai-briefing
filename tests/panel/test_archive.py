"""Archive browser tests: listing, selection, date fidelity, path safety."""

import os
import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel.app import _archive_date_str, _list_archives, app

client = TestClient(app)

FULL_MD = "# AI Briefing\n" + "\n".join(f"## Section {i}\ncontent {i}" for i in range(1, 9))


def _archive_dir(tmp_path):
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    (tmp_path / "briefing_2026-07-25_232539.md").write_text(FULL_MD)
    # part files and strays must NOT be listed
    (tmp_path / "briefing_2026-07-25_232539_part1_news.md").write_text("x")
    (tmp_path / "notes.md").write_text("x")
    return patch("panel.app.config.ARCHIVE_DIR", str(tmp_path))


def test_list_archives_full_docs_only_newest_first(tmp_path):
    with _archive_dir(tmp_path):
        entries = _list_archives()
    assert [e["file"] for e in entries] == [
        "briefing_2026-07-25_232539.md",
        "briefing_2026-07-24_0500.md",
    ]
    assert entries[0]["time"] == "23:25"  # 6-digit stamp -> HH:MM
    assert entries[1]["time"] == "05:00"  # 4-digit stamp


def test_archive_page_renders_selected_with_archives_own_date(tmp_path):
    with _archive_dir(tmp_path):
        r = client.get("/archive?view=briefing_2026-07-24_0500.md")
    assert r.status_code == 200
    # the 24th's date, NOT today's — the whole point of the archive view
    assert "Friday, July 24, 2026" in r.text
    assert r.text.count("srcdoc") == 2


def test_archive_page_defaults_to_newest(tmp_path):
    with _archive_dir(tmp_path):
        r = client.get("/archive")
    assert 'class="selected"' in r.text
    assert "briefing_2026-07-25_232539.md" in r.text.split('class="selected"')[0].rsplit("href", 1)[-1]


def test_archive_view_rejects_path_traversal(tmp_path):
    (tmp_path.parent / "secret.md").write_text("do not serve")
    with _archive_dir(tmp_path):
        r = client.get("/archive?view=../secret.md")
    assert r.status_code == 200
    assert "do not serve" not in r.text  # falls back to newest listed entry


def test_archive_empty_state(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with patch("panel.app.config.ARCHIVE_DIR", str(empty)):
        r = client.get("/archive")
    assert "The morgue file is empty" in r.text


def test_archive_date_str_is_windows_safe():
    # no %-d anywhere; renders unpadded day
    assert _archive_date_str("2026-07-05") == "Sunday, July 5, 2026"


def test_archive_list_flags_research_entries(tmp_path):
    plain = tmp_path / "briefing_2026-07-24_0500.md"
    plain.write_text(FULL_MD)
    researched = tmp_path / "briefing_2026-07-25_0600.md"
    researched.write_text(
        FULL_MD + "\n\n## 🔍 Requested Research (included in this issue)\n- https://youtu.be/abc\n",
        encoding="utf-8",
    )
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)):
        entries = _list_archives()
    by_file = {e["file"]: e["research"] for e in entries}
    assert by_file["briefing_2026-07-25_0600.md"] == ["https://youtu.be/abc"]
    assert by_file["briefing_2026-07-24_0500.md"] == []


def test_archive_list_shows_send_status(tmp_path):
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    (tmp_path / "briefing_2026-07-25_0600.md").write_text(FULL_MD)
    send_log = {
        "briefing_2026-07-25_0600.md": {
            "status": "sent", "detail": {"part1": "sent", "part2": "sent"},
            "at": "2026-07-25T06:01:00",
        }
    }
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.load_send_status", return_value=send_log):
        r = client.get("/archive")
    assert "✉ sent" in r.text          # the emailed one
    assert "draft" in r.text           # the never-emailed one


def test_record_and_load_send_status_roundtrip(tmp_path):
    from briefing import db as bdb

    with patch.object(bdb, "SEND_STATUS_PATH", str(tmp_path / "send_status.json")), \
         patch.object(bdb.config, "DATA_DIR", str(tmp_path)):
        bdb.record_send_status("briefing_x.md", {"part1": "sent", "part2": "already_sent"})
        bdb.record_send_status("briefing_y.md", {"part1": "error: boom", "part2": "sent"})
        log = bdb.load_send_status()
    assert log["briefing_x.md"]["status"] == "sent"       # already_sent counts as delivered
    assert log["briefing_y.md"]["status"] == "partial"    # one part DID deliver


def test_archive_send_button_shown_for_draft(tmp_path):
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.load_send_status", return_value={}):
        r = client.get("/archive")
    assert "draft" in r.text
    assert "Send this edition" in r.text


def test_archive_send_enqueues_job(tmp_path):
    from panel import jobs

    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.load_send_status", return_value={}), \
         patch("panel.app._archive_send_job", return_value={"part1": "sent", "part2": "sent"}):
        r = client.post("/archive/send", data={"view": "briefing_2026-07-24_0500.md"})
    assert 'hx-get="/jobs/' in r.text
    jobs.JOBS.clear()


def test_archive_send_rejects_unknown_file(tmp_path):
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)):
        r = client.post("/archive/send", data={"view": "../../../etc/passwd"})
    assert "unknown archive" in r.text


def test_archive_send_job_result_patches_list_badge_oob(tmp_path):
    """The send button lives in .archive-view; the badge it needs to update
    lives in .archive-list — two different DOM regions the #banner-targeted
    htmx swap can't reach on its own. Without the OOB fragment, a send from
    the detail pane looks like nothing happened until a full page reload."""
    from panel import app, jobs

    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.load_send_status", return_value={
             "briefing_2026-07-24_0500.md": {"status": "sent", "at": "2026-07-24T05:01:00"},
         }):
        jobs.JOBS.clear()
        job_id = "test-job-id"
        jobs.JOBS[job_id] = jobs.Job(
            name="send", status="done",
            result={"part1": "sent", "part2": "sent"},
            target="briefing_2026-07-24_0500.md",
        )
        frag = app._job_fragment(job_id)
    assert 'hx-swap-oob="true"' in frag
    assert 'id="a-badges-briefing_2026-07-24_0500-md"' in frag
    assert "just-updated" in frag
    assert "badge-sent" in frag
    assert 'id="a-send-btn-briefing_2026-07-24_0500-md"' in frag
    assert "Send again" in frag
    jobs.JOBS.clear()


def test_archive_filter_chips(tmp_path):
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)   # draft
    (tmp_path / "briefing_2026-07-25_0600.md").write_text(FULL_MD)   # sent
    send_log = {"briefing_2026-07-25_0600.md": {"status": "sent", "detail": {}, "at": "2026-07-25T06:01:00"}}
    ctx = lambda: patch("panel.app.config.ARCHIVE_DIR", str(tmp_path))
    log_ctx = lambda: patch("panel.app.db.load_send_status", return_value=send_log)

    with ctx(), log_ctx():
        r = client.get("/archive?show=sent")
    assert "briefing_2026-07-25_0600.md" in r.text
    assert "briefing_2026-07-24_0500.md" not in r.text

    with ctx(), log_ctx():
        r = client.get("/archive?show=drafts")
    assert "briefing_2026-07-24_0500.md" in r.text
    assert "briefing_2026-07-25_0600.md" not in r.text


def test_archive_unknown_view_shows_not_found_not_fallback(tmp_path):
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    with _archive_dir(tmp_path) if False else patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)):
        r = client.get("/archive?view=nonexistent.md")
    assert "No archived edition named" in r.text
    # must NOT render the newest archive as if it were the requested one
    assert r.text.count("srcdoc") == 0


def test_invalid_calendar_date_archive_skipped_not_500(tmp_path):
    # hunt-panel HIGH#1: shape-valid but calendar-invalid filename must not
    # crash the whole tab when auto-selected as newest.
    (tmp_path / "briefing_2026-13-45_9999.md").write_text(FULL_MD)
    (tmp_path / "briefing_2026-07-24_0500.md").write_text(FULL_MD)
    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)):
        r = client.get("/archive")
    assert r.status_code == 200
    assert "briefing_2026-13-45_9999.md" not in r.text  # skipped from list
    assert "briefing_2026-07-24_0500.md" in r.text      # valid one renders


def test_date_headers_are_not_sticky():
    """Regression: every date-group header used position:sticky top:0,
    so scrolling past group N made group N+1's header overlap/replace it —
    the reported 'date time pane got overlapped' bug. Assert against the
    stylesheet since this is a rendering property, not markup."""
    css_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src", "panel", "static", "panel.css",
    )
    with open(css_path) as f:
        css = f.read()
    datehead_rule = re.search(r"\.a-datehead\s*\{[^}]*\}", css)
    assert datehead_rule, "no .a-datehead rule found"
    assert "sticky" not in datehead_rule.group(0)
