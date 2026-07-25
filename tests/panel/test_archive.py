"""Archive browser tests: listing, selection, date fidelity, path safety."""

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
    assert "No archived briefings yet" in r.text


def test_archive_date_str_is_windows_safe():
    # no %-d anywhere; renders unpadded day
    assert _archive_date_str("2026-07-05") == "Sunday, July 5, 2026"
