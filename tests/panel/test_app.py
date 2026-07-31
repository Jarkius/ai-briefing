"""S1 tests: app shell, status route, and the two structural invariants —
localhost-only launcher and the panel→briefing one-way import direction.
"""

import os
import subprocess
import sys

from fastapi.testclient import TestClient

from panel.app import app

client = TestClient(app)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_index_redirects_to_preview():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/preview"


def test_preview_renders_shell():
    r = client.get("/preview")
    assert r.status_code == 200
    assert "Preview" in r.text


def test_status_idle_when_no_jobs_and_no_lock():
    from unittest.mock import patch

    with patch("panel.app.mcp_client.is_locked", return_value=False):
        r = client.get("/status")
    assert r.status_code == 200
    assert "dot-idle" in r.text
    assert 'hx-trigger="every 3s"' in r.text


def test_status_shows_lock_when_cron_holds_it():
    from unittest.mock import patch

    with patch("panel.app.mcp_client.is_locked", return_value=True):
        r = client.get("/status")
    assert "dot-lock" in r.text


def test_status_shows_busy_when_job_running():
    from unittest.mock import patch

    from panel import jobs

    jobs.JOBS["test123"] = jobs.Job(name="probe")
    try:
        with patch("panel.app.mcp_client.is_locked", return_value=False):
            r = client.get("/status")
        assert "dot-busy" in r.text
    finally:
        del jobs.JOBS["test123"]


# ---- structural invariants ---------------------------------------------------


def test_briefing_package_never_imports_panel():
    # AC13's decoupling guarantee: no import statement in the core package
    # may pull in the panel package (comments mentioning it are fine).
    import ast

    briefing_dir = os.path.join(REPO_ROOT, "src", "briefing")
    for fname in os.listdir(briefing_dir):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(briefing_dir, fname), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            assert not any(n.split(".")[0] == "panel" for n in names), (
                f"src/briefing/{fname} imports the panel package"
            )


def test_briefing_imports_without_fastapi():
    # AC13: core must import in an env where fastapi is unimportable.
    # Simulate by blocking the module, in a subprocess for a clean slate.
    code = (
        "import sys; sys.path.insert(0, 'src'); "
        "sys.modules['fastapi'] = None; "
        "import briefing.collector, briefing.generator, briefing.sender, "
        "briefing.researcher, briefing.db, briefing.config, briefing.gmail_api; "
        "print('ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_panel_sh_binds_localhost_only():
    # AC2 at the source: the launcher must pin 127.0.0.1.
    with open(os.path.join(REPO_ROOT, "panel.sh")) as f:
        script = f.read()
    assert "--host 127.0.0.1" in script
    assert "0.0.0.0" not in script


# ---- _send_job -----------------------------------------------------------------


def test_send_job_orchestrates_sender_and_records_status(tmp_path, monkeypatch):
    import sqlite3
    from unittest.mock import patch

    from briefing import db as bdb
    from panel import state
    from panel.app import _send_job

    FAKE_GEN = {
        "part1_html": "<html>P1</html>", "part2_html": "<html>P2</html>",
        "date_str": "Saturday, July 25, 2026", "archive_file": "briefing_2026-07-25_0500.md",
    }
    state.set_generation(FAKE_GEN)

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    recorded = {}

    def fake_record_send_status(archive_file, result):
        recorded["archive_file"] = archive_file
        recorded["result"] = result

    try:
        with patch("panel.app.db.connect", side_effect=fake_connect), \
             patch("panel.app.sender.send_two_part_briefing",
                   return_value={"part1": "sent", "part2": "sent"}) as send_mock, \
             patch("panel.app.db.record_send_status", side_effect=fake_record_send_status):
            result = _send_job()
    finally:
        state.LAST_GENERATION = None

    send_mock.assert_called_once_with("<html>P1</html>", "<html>P2</html>", "Saturday, July 25, 2026")
    assert result == {"part1": "sent", "part2": "sent"}
    assert recorded == {"archive_file": "briefing_2026-07-25_0500.md",
                         "result": {"part1": "sent", "part2": "sent"}}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT source, send_status FROM runs").fetchone()
    assert row["source"] == "dashboard"
    assert row["send_status"] == "ok"


def test_send_job_records_error_status_when_a_part_fails(tmp_path):
    import sqlite3
    from unittest.mock import patch

    from briefing import db as bdb
    from panel import state
    from panel.app import _send_job

    FAKE_GEN = {
        "part1_html": "<html>P1</html>", "part2_html": "<html>P2</html>",
        "date_str": "Saturday, July 25, 2026",
    }
    state.set_generation(FAKE_GEN)

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    try:
        with patch("panel.app.db.connect", side_effect=fake_connect), \
             patch("panel.app.sender.send_two_part_briefing",
                   return_value={"part1": "sent", "part2": "error: smtp down"}):
            result = _send_job()
    finally:
        state.LAST_GENERATION = None

    assert result == {"part1": "sent", "part2": "error: smtp down"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT send_status, error_text FROM runs").fetchone()
    assert row["send_status"] == "error"
    assert "error: smtp down" in row["error_text"]


def test_send_job_raises_when_nothing_generated():
    import pytest

    from panel import state
    from panel.app import _send_job

    state.LAST_GENERATION = None
    with pytest.raises(RuntimeError, match="nothing generated yet"):
        _send_job()


# ---- _pathspec_commit -----------------------------------------------------------


def _init_git_repo(path):
    subprocess.run(["git", "init"], cwd=path, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def test_pathspec_commit_commits_a_real_change(tmp_path):
    from unittest.mock import patch

    from panel.app import _pathspec_commit

    _init_git_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("hello")

    with patch("panel.app.config.REPO_ROOT", str(tmp_path)):
        err = _pathspec_commit("test: add tracked file", str(target))

    assert err is None
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True, check=True)
    assert "test: add tracked file" in log.stdout


def test_pathspec_commit_nothing_to_commit_is_not_an_error(tmp_path):
    from unittest.mock import patch

    from panel.app import _pathspec_commit

    _init_git_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("hello")

    with patch("panel.app.config.REPO_ROOT", str(tmp_path)):
        first = _pathspec_commit("test: initial commit", str(target))
        assert first is None
        # no changes since — must not raise or report an error
        second = _pathspec_commit("test: no-op commit", str(target))

    assert second is None


def test_pathspec_commit_catches_exception_and_returns_message(tmp_path):
    from unittest.mock import patch

    from panel.app import _pathspec_commit

    # not a git repo at all -> `git add` fails with non-zero exit under
    # check=True, raising CalledProcessError, which the except must catch.
    target = tmp_path / "orphan.txt"
    target.write_text("hello")

    with patch("panel.app.config.REPO_ROOT", str(tmp_path)):
        err = _pathspec_commit("test: should fail", str(target))

    assert err is not None
    assert isinstance(err, str)


# ---- sources_page / schedule_page (GET) -----------------------------------------


def test_sources_page_renders_current_subscriptions(tmp_path):
    import json
    from unittest.mock import patch

    subs = tmp_path / "subscriptions.json"
    subs.write_text(json.dumps([{"source_type": "news", "identifier": "https://x.com/feed", "name": "X Blog"}]))
    with patch("panel.app.config.SUBSCRIPTIONS_PATH", str(subs)):
        r = client.get("/sources")
    assert r.status_code == 200
    assert "X Blog" in r.text
    assert "https://x.com/feed" in r.text


def test_schedule_page_shows_current_time_on_darwin(tmp_path):
    import plistlib
    from unittest.mock import patch

    plist_path = tmp_path / "com.user.ai-briefing.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump({"StartCalendarInterval": {"Hour": 7, "Minute": 15}}, f)

    with patch("panel.app.PLIST_PATH", str(plist_path)), \
         patch("panel.app.sys.platform", "darwin"):
        r = client.get("/schedule")
    assert r.status_code == 200
    assert 'value="7"' in r.text
    assert 'value="15"' in r.text


def test_schedule_page_shows_error_on_non_darwin():
    from unittest.mock import patch

    with patch("panel.app.sys.platform", "win32"):
        r = client.get("/schedule")
    assert r.status_code == 200
    assert "macOS/launchd-only" in r.text


# ---- _archive_send_job -----------------------------------------------------------


def test_archive_send_job_orchestrates_sender_and_records_status(tmp_path):
    import sqlite3
    from unittest.mock import patch

    from briefing import db as bdb
    from panel.app import _archive_send_job

    archive = tmp_path / "briefing_2026-07-24_0500.md"
    archive.write_text("# AI Briefing\n" + "\n".join(f"## Section {i}\ncontent {i}" for i in range(1, 9)))

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    recorded = {}

    def fake_record_send_status(fname, result):
        recorded["fname"] = fname
        recorded["result"] = result

    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.sender.send_two_part_briefing",
               return_value={"part1": "sent", "part2": "sent"}) as send_mock, \
         patch("panel.app.db.record_send_status", side_effect=fake_record_send_status):
        result = _archive_send_job("briefing_2026-07-24_0500.md", "2026-07-24")

    assert send_mock.call_count == 1
    assert result == {"part1": "sent", "part2": "sent"}
    assert recorded["fname"] == "briefing_2026-07-24_0500.md"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT source, send_status FROM runs").fetchone()
    assert row["source"] == "dashboard"
    assert row["send_status"] == "ok"


def test_archive_send_job_records_error_status(tmp_path):
    import sqlite3
    from unittest.mock import patch

    from briefing import db as bdb
    from panel.app import _archive_send_job

    archive = tmp_path / "briefing_2026-07-24_0500.md"
    archive.write_text("# AI Briefing\n" + "\n".join(f"## Section {i}\ncontent {i}" for i in range(1, 9)))

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
         patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.sender.send_two_part_briefing",
               return_value={"part1": "error: boom", "part2": "sent"}), \
         patch("panel.app.db.record_send_status"):
        result = _archive_send_job("briefing_2026-07-24_0500.md", "2026-07-24")

    assert result == {"part1": "error: boom", "part2": "sent"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT send_status, error_text FROM runs").fetchone()
    assert row["send_status"] == "error"


# ---- _regenerate_job exception branch --------------------------------------------


def test_regenerate_job_sets_error_status_and_reraises(tmp_path):
    import sqlite3

    import pytest
    from unittest.mock import patch

    from briefing import db as bdb
    from panel.app import _regenerate_job

    db_path = str(tmp_path / "feeds.db")

    def fake_connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        bdb._ensure_runs_table(conn)
        return conn

    with patch("panel.app.db.connect", side_effect=fake_connect), \
         patch("panel.app.generator.generate", side_effect=RuntimeError("model unavailable")):
        with pytest.raises(RuntimeError, match="model unavailable"):
            _regenerate_job()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT generate_status, error_text FROM runs").fetchone()
    assert row["generate_status"] == "error"
    assert "model unavailable" in row["error_text"]


# ---- _job_fragment edge cases -----------------------------------------------------


def test_job_fragment_none_job_renders_unknown_job_banner():
    from panel.app import _job_fragment

    frag = _job_fragment("no-such-job-id")
    assert "no longer exists" in frag
    assert "every 2s" not in frag  # terminal — must not keep polling a dead job


def test_job_fragment_generic_done_job_falls_through_to_generic_banner():
    from panel import jobs
    from panel.app import _job_fragment

    j = jobs.Job(name="paste", status="done")
    jobs.JOBS["genericdone"] = j
    try:
        frag = _job_fragment("genericdone")
    finally:
        del jobs.JOBS["genericdone"]
    assert "paste done" in frag
    assert "banner-ok" in frag


# ---- _research_job git-commit-failed branch ---------------------------------------


def test_research_job_appends_commit_failure_note_to_findings():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from panel.app import _research_job

    class FakeLock:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return None

    with patch("panel.app.mcp_client.mcp_lock", return_value=FakeLock()), \
         patch("panel.app.researcher.run_pending_async", new=AsyncMock(return_value=("findings text", 1))), \
         patch("panel.app._pathspec_commit", return_value="dirty index"):
        result = asyncio.run(_research_job(lambda t: None))

    assert "findings text" in result
    assert "git commit failed: dirty index" in result


# ---- _phase_strip db.connect() failure fallback --------------------------------


def test_phase_strip_falls_back_to_placeholders_on_db_error():
    from unittest.mock import patch

    from panel.app import _phase_strip

    with patch("panel.app.db.connect", side_effect=RuntimeError("db unavailable")):
        strip = _phase_strip()

    assert len(strip) == 4
    for entry in strip:
        assert entry["status"] == "—"
        assert entry["when"] == ""
        assert entry["source"] == ""


# ---- _archive_date_str ValueError fallback --------------------------------------


def test_archive_date_str_returns_raw_string_on_unparseable_date():
    from panel.app import _archive_date_str

    assert _archive_date_str("not-a-date") == "not-a-date"


# ---- _archive_research_labels OSError branch ------------------------------------


def test_archive_research_labels_returns_empty_on_oserror(tmp_path):
    from unittest.mock import patch

    from panel.app import _archive_research_labels

    target = tmp_path / "briefing_2026-07-24_0500.md"
    target.write_text("content")

    with patch("builtins.open", side_effect=OSError("boom")):
        labels = _archive_research_labels(str(target))

    assert labels == []


# ---- schedule_save additional branches ------------------------------------------


def test_schedule_save_rejects_non_darwin_platform():
    from unittest.mock import patch

    with patch("panel.app.sys.platform", "win32"):
        r = client.post("/schedule", data={"hour": "6", "minute": "0"})
    assert "macOS" in r.text


def test_schedule_save_rejects_missing_plist(tmp_path):
    from unittest.mock import patch

    missing_plist = tmp_path / "does_not_exist.plist"
    with patch("panel.app.PLIST_PATH", str(missing_plist)), \
         patch("panel.app.sys.platform", "darwin"):
        r = client.post("/schedule", data={"hour": "6", "minute": "0"})
    assert "plist not found" in r.text


# ---- _read_env_lines --------------------------------------------------------------


def test_read_env_lines_returns_empty_list_when_file_missing(tmp_path):
    from unittest.mock import patch

    from panel.app import _read_env_lines

    missing = tmp_path / "does_not_exist.env"
    with patch("panel.app.config.ENV_PATH", str(missing)):
        assert _read_env_lines() == []


# ---- archive_send reattach-to-running-job branch --------------------------------


def test_archive_send_reattaches_to_running_job(tmp_path):
    from unittest.mock import patch

    from panel import jobs

    (tmp_path / "briefing_2026-07-24_0500.md").write_text(
        "# AI Briefing\n" + "\n".join(f"## Section {i}\ncontent {i}" for i in range(1, 9))
    )
    jobs.JOBS.clear()
    jobs.JOBS["archivesendbusy"] = jobs.Job(name="send")  # running
    try:
        with patch("panel.app.config.ARCHIVE_DIR", str(tmp_path)), \
             patch("panel.app.db.load_send_status", return_value={}):
            r = client.post("/archive/send", data={"view": "briefing_2026-07-24_0500.md"})
        assert "archivesendbusy" in r.text   # reattached to the existing job
        assert len(jobs.JOBS) == 1           # no second job spawned
    finally:
        jobs.JOBS.clear()


# ---- _provider_status --------------------------------------------------------------


def test_provider_status_bedrock_enabled():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.BEDROCK_ENABLED", True):
        assert _provider_status("bedrock") == "enabled"


def test_provider_status_bedrock_disabled():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.BEDROCK_ENABLED", False):
        assert _provider_status("bedrock") == "disabled"


def test_provider_status_gemini_key_set():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.GEMINI_API_KEY", "sk-xyz"):
        assert _provider_status("gemini") == "key set"


def test_provider_status_gemini_no_key():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.GEMINI_API_KEY", ""):
        assert _provider_status("gemini") == "no key"


def test_provider_status_maxplus_key_set():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.MAXPLUS_API_KEY", "mp-key"):
        assert _provider_status("maxplus") == "key set"


def test_provider_status_maxplus_no_key():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.MAXPLUS_API_KEY", ""):
        assert _provider_status("maxplus") == "no key"


def test_provider_status_claude_cli_installed():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.CLAUDE_CLI_ENABLED", True), \
         patch("shutil.which", return_value="/usr/local/bin/claude"):
        assert _provider_status("claude-cli") == "installed"


def test_provider_status_claude_cli_not_on_path():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.CLAUDE_CLI_ENABLED", True), \
         patch("shutil.which", return_value=None):
        assert _provider_status("claude-cli") == "not on PATH"


def test_provider_status_claude_cli_disabled():
    from unittest.mock import patch

    from panel.app import _provider_status

    with patch("panel.app.config.CLAUDE_CLI_ENABLED", False):
        assert _provider_status("claude-cli") == "disabled"
