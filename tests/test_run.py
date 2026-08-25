"""Tests for run.py: the 4-phase CLI orchestrator (collect -> research ->
generate -> send).

run.py lives at the repo root, not under src/, so it isn't on sys.path via
the editable install of the `briefing` package — insert the repo root here
(same pattern run.py itself uses for src/) rather than touching conftest.py.

Every collaborator (collector, researcher, generator, sender, db) is
monkeypatched — no real network, email, AI, or database calls — EXCEPT in
the "real generate()" section below, which deliberately leaves
generator.generate() unstubbed (only its LLM call and archive dir are
patched) to prove the Part 3 research-archive write introduced alongside
the dashboard's research-preview tabs actually coexists with a real
two-part send on the unattended cron path, not just in generator.py's own
unit tests.
"""

import functools
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import run  # noqa: E402
from briefing import config as briefing_config  # noqa: E402


class _Recorder:
    """Captures db.* calls made through run.main() without touching sqlite."""

    def __init__(self):
        self.conn = MagicMock(name="conn")
        self.run_id = 42
        self.insert_run = MagicMock(return_value=self.run_id)
        self.update_calls = []
        self.record_send_status = MagicMock()

    def update_run(self, conn_arg, run_id, **fields):
        assert conn_arg is self.conn
        self.update_calls.append((run_id, fields))

    def status_for(self, key):
        """Value passed for `key` in the most recent update_run call that set it."""
        for run_id, fields in reversed(self.update_calls):
            if key in fields:
                return fields[key]
        return None


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run.config, "require_env", lambda: None)
    monkeypatch.setattr(run.db, "connect", lambda: r.conn)
    monkeypatch.setattr(run.db, "insert_run", r.insert_run)
    monkeypatch.setattr(run.db, "update_run", r.update_run)
    monkeypatch.setattr(run.db, "record_send_status", r.record_send_status)
    # Default Phase 5 to a deterministic no-op ("no fetchable sources") so
    # tests that don't care about the social post don't depend on a
    # MagicMock conn behaving like a real sqlite connection.
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [])
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", lambda items, max_items=None: [])
    monkeypatch.setattr(run, "wait_for_network", lambda: True)
    return r


def _generate_result(**overrides):
    result = {
        "part1_html": "<p>one</p>",
        "part2_html": "<p>two</p>",
        "date_str": "Thursday, July 30, 2026",
        "archive_file": "briefing_2026-07-30_0800.md",
    }
    result.update(overrides)
    return result


# ---- happy path -------------------------------------------------------------


def test_main_happy_path_runs_all_phases_in_order(rec, monkeypatch):
    call_order = []

    def fake_collect(run_id, conn):
        call_order.append("collect")
        assert run_id == rec.run_id
        assert conn is rec.conn
        return "ok"

    def fake_run_pending():
        call_order.append("research")
        return "some findings", 3

    generate_result = _generate_result()

    def fake_generate(conn, research_findings=""):
        call_order.append("generate")
        assert conn is rec.conn
        assert research_findings == "some findings"
        return generate_result

    send_result = {"part1": "sent", "part2": "sent"}

    def fake_send(part1_html, part2_html, date_str):
        call_order.append("send")
        assert part1_html == "<p>one</p>"
        assert part2_html == "<p>two</p>"
        assert date_str == "Thursday, July 30, 2026"
        return send_result

    def fake_candidates(conn):
        call_order.append("social_post_candidates")
        return [{"title": "x", "url": "https://example.com"}]

    def fake_deep_fetch(items, max_items=None):
        call_order.append("deep_fetch")
        return [{"title": "x", "url": "https://example.com", "content": "body"}]

    def fake_build_source(fetched):
        call_order.append("build_source")
        return "source material"

    def fake_generate_social_post(source_material, date_str):
        call_order.append("generate_social_post")
        return "post text"

    def fake_render_social_post_html(post_text, date_str):
        call_order.append("render_social_post_html")
        return "<p>post text</p>"

    def fake_send_social_post_email(post_html, date_str):
        call_order.append("send_social_post_email")
        return "sent"

    monkeypatch.setattr(run.collector, "run", fake_collect)
    monkeypatch.setattr(run.researcher, "run_pending", fake_run_pending)
    monkeypatch.setattr(run.generator, "generate", fake_generate)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", fake_send)
    monkeypatch.setattr(run.generator, "social_post_candidate_items", fake_candidates)
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", fake_deep_fetch)
    monkeypatch.setattr(run.generator, "build_social_post_source", fake_build_source)
    monkeypatch.setattr(run.generator, "generate_social_post", fake_generate_social_post)
    monkeypatch.setattr(run.sender, "render_social_post_html", fake_render_social_post_html)
    monkeypatch.setattr(run.sender, "send_social_post_email", fake_send_social_post_email)

    run.main()

    assert call_order == [
        "collect", "research", "generate", "send",
        "social_post_candidates", "deep_fetch", "build_source",
        "generate_social_post", "render_social_post_html", "send_social_post_email",
    ]
    rec.conn.close.assert_called_once()
    assert rec.status_for("collect_status") == "ok"
    assert rec.status_for("research_status") == "ok (3 processed)"
    assert rec.status_for("generate_status") == "ok"
    assert rec.status_for("send_status") == str(send_result)
    assert rec.status_for("social_post_status") == "sent"
    assert rec.status_for("finished_at") is not None
    rec.record_send_status.assert_called_once_with(generate_result["archive_file"], send_result)


def test_main_inserts_run_row_with_cron_source(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": None)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", MagicMock())

    run.main()

    rec.insert_run.assert_called_once()
    args, kwargs = rec.insert_run.call_args
    assert args[0] is rec.conn
    assert kwargs["source"] == "cron"
    assert "started_at" in kwargs


def test_main_research_status_reports_nothing_pending_when_count_zero(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))

    generate_findings = []

    def fake_generate(conn, research_findings=""):
        generate_findings.append(research_findings)
        return _generate_result()

    monkeypatch.setattr(run.generator, "generate", fake_generate)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", lambda p1, p2, d: {"part1": "sent", "part2": "sent"})

    run.main()

    assert rec.status_for("research_status") == "ok (nothing pending)"
    assert generate_findings == [""]


# ---- soft-fail branches ------------------------------------------------------


def test_main_collect_phase_failure_is_caught_and_pipeline_continues(rec, monkeypatch):
    def fake_collect(run_id, conn):
        raise RuntimeError("feed down")

    research_called = []
    generate_findings = []
    send_called = []

    def fake_run_pending():
        research_called.append(True)
        return "findings", 1

    def fake_generate(conn, research_findings=""):
        generate_findings.append(research_findings)
        return _generate_result()

    def fake_send(part1_html, part2_html, date_str):
        send_called.append(True)
        return {"part1": "sent", "part2": "sent"}

    monkeypatch.setattr(run.collector, "run", fake_collect)
    monkeypatch.setattr(run.researcher, "run_pending", fake_run_pending)
    monkeypatch.setattr(run.generator, "generate", fake_generate)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", fake_send)

    run.main()

    assert rec.status_for("collect_status") == "error: feed down"
    # later phases still ran despite the collect failure
    assert research_called == [True]
    assert generate_findings == ["findings"]
    assert send_called == [True]


def test_main_research_phase_failure_is_caught_and_pipeline_continues(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")

    def fake_run_pending():
        raise RuntimeError("google blocked")

    generate_findings = []
    send_called = []

    def fake_generate(conn, research_findings=""):
        generate_findings.append(research_findings)
        return _generate_result()

    def fake_send(part1_html, part2_html, date_str):
        send_called.append(True)
        return {"part1": "sent", "part2": "sent"}

    monkeypatch.setattr(run.researcher, "run_pending", fake_run_pending)
    monkeypatch.setattr(run.generator, "generate", fake_generate)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", fake_send)

    run.main()

    assert rec.status_for("research_status") == "error: google blocked"
    # research_findings never got assigned past its "" default, so generate
    # still ran — with no findings, not with stale data from a prior run
    assert generate_findings == [""]
    assert send_called == [True]


def test_main_generate_phase_failure_skips_send(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))

    def fake_generate(conn, research_findings=""):
        raise RuntimeError("gemini down")

    send_mock = MagicMock()
    monkeypatch.setattr(run.generator, "generate", fake_generate)
    monkeypatch.setattr(run.sender, "send_two_part_briefing", send_mock)

    run.main()

    assert rec.status_for("generate_status") == "error: gemini down"
    # result stayed None, so the send phase's `if result:` guard never opens
    assert rec.status_for("send_status") == "skipped"
    send_mock.assert_not_called()
    rec.record_send_status.assert_not_called()


def test_main_send_phase_failure_records_error_status(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": _generate_result())

    def fake_send(part1_html, part2_html, date_str):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(run.sender, "send_two_part_briefing", fake_send)

    run.main()

    assert rec.status_for("send_status") == "error: smtp down"
    rec.record_send_status.assert_not_called()


# ---- --dry-run ----------------------------------------------------------------


def test_main_dry_run_skips_send_and_prints_preview(rec, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry-run"])
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(
        run.generator,
        "generate",
        lambda conn, research_findings="": _generate_result(
            part1_html="<p>PART ONE CONTENT</p>", part2_html="<p>PART TWO CONTENT</p>"
        ),
    )
    send_mock = MagicMock()
    monkeypatch.setattr(run.sender, "send_two_part_briefing", send_mock)

    run.main()

    out = capsys.readouterr().out
    assert "PART 1" in out
    assert "PART 2" in out
    assert "PART ONE CONTENT" in out
    assert "PART TWO CONTENT" in out
    send_mock.assert_not_called()
    rec.record_send_status.assert_not_called()
    assert rec.status_for("send_status") == "dry_run"


# ---- db.record_send_status -----------------------------------------------------


def test_main_records_send_status_with_archive_file_and_send_result(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(
        run.generator,
        "generate",
        lambda conn, research_findings="": _generate_result(archive_file="briefing_2026-07-30_0800.md"),
    )
    send_result = {"part1": "sent", "part2": "already_sent"}
    monkeypatch.setattr(run.sender, "send_two_part_briefing", lambda p1, p2, d: send_result)

    run.main()

    rec.record_send_status.assert_called_once_with("briefing_2026-07-30_0800.md", send_result)
    assert rec.status_for("send_status") == str(send_result)


def test_main_skips_record_send_status_when_no_archive_file(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    result_without_archive = _generate_result()
    del result_without_archive["archive_file"]
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": result_without_archive)
    monkeypatch.setattr(
        run.sender, "send_two_part_briefing", lambda p1, p2, d: {"part1": "sent", "part2": "sent"}
    )

    run.main()

    rec.record_send_status.assert_not_called()


# ---- wait_for_network ---------------------------------------------------------


def test_wait_for_network_true_on_first_successful_connection(monkeypatch):
    monkeypatch.setattr(run.socket, "create_connection", MagicMock())
    assert run.wait_for_network(timeout=5) is True


def test_wait_for_network_false_when_connection_always_fails(monkeypatch):
    monkeypatch.setattr(run.socket, "create_connection", MagicMock(side_effect=OSError("unreachable")))
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    assert run.wait_for_network(timeout=1) is False


def test_wait_for_network_recovers_after_transient_failure(monkeypatch):
    attempts = [OSError("unreachable"), MagicMock()]

    def fake_create_connection(*args, **kwargs):
        result = attempts.pop(0)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(run.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    assert run.wait_for_network(timeout=10) is True


def test_main_logs_network_check_ok(rec, monkeypatch, capsys):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": _generate_result())
    monkeypatch.setattr(run.sender, "send_two_part_briefing", lambda p1, p2, d: {"part1": "sent", "part2": "sent"})

    run.main()

    assert "Network check: ok" in capsys.readouterr().out


def test_main_logs_and_continues_when_network_check_fails(rec, monkeypatch, capsys):
    monkeypatch.setattr(run, "wait_for_network", lambda: False)
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": _generate_result())
    monkeypatch.setattr(run.sender, "send_two_part_briefing", lambda p1, p2, d: {"part1": "sent", "part2": "sent"})

    run.main()

    out = capsys.readouterr().out
    assert "no connectivity" in out
    # the pipeline still runs to completion despite the failed check
    assert rec.status_for("send_status") == "{'part1': 'sent', 'part2': 'sent'}"


# ---- real generate(): Part 3 research archive on the actual cron path --------


def test_main_real_generate_writes_part3_archive_and_sends_only_two_real_parts(tmp_path, monkeypatch):
    """Unlike every other test in this file, generator.generate() itself is
    NOT stubbed here — only its LLM call and archive dir are. This is the
    one test that runs research_findings all the way through the real
    generate() on run.main()'s actual cron path, proving: (1) the Part 3
    research archive gets written to disk during a real run when research
    is pending (not just in the dashboard), (2) sender.send_two_part_briefing
    still receives exactly the two real parts with no Part 3 leakage, and
    (3) the full/part1/part2/part3 archive files coexist with no path
    collision."""
    conn = sqlite3.connect(tmp_path / "feeds.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE feed_items (
            title TEXT, content TEXT, url TEXT, source_type TEXT,
            published_at TEXT, fetched_at TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO feed_items (title, content, url, source_type, published_at, fetched_at) "
        "VALUES ('Item', 'c', 'http://a', 'news', '2026-07-30', datetime('now', '-1 hours'))"
    )
    conn.commit()

    archive_dir = tmp_path / "archives"
    fake_markdown = "# AI Briefing\n" + "\n\n---\n\n".join(
        f"## Section {i}\ncontent for section {i}" for i in range(1, 7)
    )

    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setattr(run.config, "require_env", lambda: None)
    monkeypatch.setattr(run.db, "connect", lambda: conn)
    monkeypatch.setattr(run.db, "insert_run", lambda conn_arg, **kwargs: 1)
    update_calls = []
    monkeypatch.setattr(
        run.db, "update_run", lambda conn_arg, run_id, **fields: update_calls.append(fields)
    )
    record_send_status_mock = MagicMock()
    monkeypatch.setattr(run.db, "record_send_status", record_send_status_mock)
    monkeypatch.setattr(run, "wait_for_network", lambda: True)
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(
        run.researcher, "run_pending", lambda: ("### My Topic\nSome findings text.\n", 1)
    )
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [])
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", lambda items, max_items=None: [])

    send_calls = []

    def fake_send(part1_html, part2_html, date_str):
        send_calls.append((part1_html, part2_html, date_str))
        return {"part1": "sent", "part2": "sent"}

    monkeypatch.setattr(run.sender, "send_two_part_briefing", fake_send)

    with patch.object(briefing_config, "ARCHIVE_DIR", str(archive_dir)), \
         patch.object(briefing_config, "STYLE_PATH", str(tmp_path / "no_such_style.md")), \
         patch("briefing.generator.call_gemini", return_value=fake_markdown), \
         patch("briefing.generator.open", functools.partial(open, encoding="utf-8")):
        run.main()

    # the real send path received exactly one call with exactly the two
    # real HTML parts + date_str — no part3 argument leaked into it
    assert len(send_calls) == 1
    assert len(send_calls[0]) == 3

    archived = {p.name for p in archive_dir.iterdir()}
    assert any(n.endswith("_part3_research.md") for n in archived)
    assert any(n.endswith("_part1_news.md") for n in archived)
    assert any(n.endswith("_part2_technical.md") for n in archived)
    full_archives = [
        n for n in archived
        if n.startswith("briefing_")
        and n.endswith(".md")
        and not n.endswith(("_part1_news.md", "_part2_technical.md", "_part3_research.md"))
    ]
    assert len(full_archives) == 1  # no collision between the full and Part 3 archive names

    record_send_status_mock.assert_called_once()


# ---- Phase 5: social post -----------------------------------------------------


def _happy_path_through_send(monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))
    monkeypatch.setattr(run.generator, "generate", lambda conn, research_findings="": _generate_result())
    monkeypatch.setattr(run.sender, "send_two_part_briefing", lambda p1, p2, d: {"part1": "sent", "part2": "sent"})


def test_main_social_post_skipped_when_no_fetchable_sources(rec, monkeypatch):
    _happy_path_through_send(monkeypatch)
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [{"title": "x", "url": ""}])
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", lambda items, max_items=None: [])
    send_social_post_mock = MagicMock()
    monkeypatch.setattr(run.sender, "send_social_post_email", send_social_post_mock)

    run.main()

    assert rec.status_for("social_post_status") == "skipped (no fetchable sources)"
    send_social_post_mock.assert_not_called()


def test_main_social_post_skipped_when_generate_phase_failed(rec, monkeypatch):
    monkeypatch.setattr(run.collector, "run", lambda run_id, conn: "ok")
    monkeypatch.setattr(run.researcher, "run_pending", lambda: ("", 0))

    def fake_generate(conn, research_findings=""):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(run.generator, "generate", fake_generate)
    candidates_mock = MagicMock()
    monkeypatch.setattr(run.generator, "social_post_candidate_items", candidates_mock)

    run.main()

    # result stayed None (generate failed), so Phase 5's `if result:` guard
    # never opens — matches Phase 4's send-skip behavior on the same failure
    assert rec.status_for("social_post_status") == "skipped"
    candidates_mock.assert_not_called()


def test_main_social_post_sent_end_to_end(rec, monkeypatch):
    _happy_path_through_send(monkeypatch)
    fetched = [{"title": "Article", "url": "https://example.com/a", "content": "body"}]
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [{"title": "Article", "url": "https://example.com/a"}])
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", lambda items, max_items=None: fetched)
    monkeypatch.setattr(run.generator, "build_social_post_source", lambda f: "source material")
    monkeypatch.setattr(run.generator, "generate_social_post", lambda source, date_str: "generated post")
    monkeypatch.setattr(run.sender, "render_social_post_html", lambda text, date_str: "<p>generated post</p>")
    send_social_post_mock = MagicMock(return_value="sent")
    monkeypatch.setattr(run.sender, "send_social_post_email", send_social_post_mock)

    run.main()

    assert rec.status_for("social_post_status") == "sent"
    send_social_post_mock.assert_called_once_with("<p>generated post</p>", "Thursday, July 30, 2026")


def test_main_social_post_failure_is_caught_soft_and_does_not_affect_send_status(rec, monkeypatch):
    _happy_path_through_send(monkeypatch)
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [{"title": "x", "url": "https://example.com"}])

    def fake_deep_fetch(items, max_items=None):
        raise RuntimeError("mcp session boom")

    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", fake_deep_fetch)

    run.main()

    assert rec.status_for("social_post_status") == "error: mcp session boom"
    # the main send phase's own status is untouched by the later failure
    assert rec.status_for("send_status") == "{'part1': 'sent', 'part2': 'sent'}"
    rec.conn.close.assert_called_once()


def test_main_social_post_dry_run_prints_preview_and_does_not_send(rec, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry-run"])
    _happy_path_through_send(monkeypatch)
    monkeypatch.setattr(run.generator, "social_post_candidate_items", lambda conn: [{"title": "x", "url": "https://example.com"}])
    monkeypatch.setattr(run.researcher, "deep_fetch_items_sync", lambda items, max_items=None: [
        {"title": "x", "url": "https://example.com", "content": "body"}
    ])
    monkeypatch.setattr(run.generator, "build_social_post_source", lambda f: "source material")
    monkeypatch.setattr(run.generator, "generate_social_post", lambda source, date_str: "PREVIEW POST TEXT")
    send_social_post_mock = MagicMock()
    monkeypatch.setattr(run.sender, "send_social_post_email", send_social_post_mock)

    run.main()

    out = capsys.readouterr().out
    assert "SOCIAL POST" in out
    assert "PREVIEW POST TEXT" in out
    assert rec.status_for("social_post_status") == "dry_run"
    send_social_post_mock.assert_not_called()
