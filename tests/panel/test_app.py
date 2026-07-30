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
        with open(os.path.join(briefing_dir, fname)) as f:
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
