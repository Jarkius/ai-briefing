"""Sources page tests: listing, add, and the enable/disable toggle."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from panel.app import app

client = TestClient(app)


def _subs(patched):
    return patch("panel.app.config.load_subscriptions", return_value=patched)


# ---- GET /sources — status column rendering ------------------------------------


def test_sources_page_healthy_source_shows_no_badge():
    subs = [{"source_type": "news", "identifier": "x", "name": "TechCrunch"}]
    with _subs(subs):
        r = client.get("/sources")
    assert "TechCrunch" in r.text
    assert "badge-flaky" not in r.text
    assert "disabled" not in r.text


def test_sources_page_below_warn_threshold_shows_muted_count_not_badge():
    subs = [{"source_type": "news", "identifier": "x", "name": "TechCrunch", "consecutive_failures": 1}]
    with _subs(subs):
        r = client.get("/sources")
    assert "1 recent failure" in r.text
    assert "badge-flaky" not in r.text


def test_sources_page_at_warn_threshold_shows_flaky_badge():
    subs = [{"source_type": "news", "identifier": "x", "name": "Reddit r/MachineLearning", "consecutive_failures": 3}]
    with _subs(subs):
        r = client.get("/sources")
    assert 'class="badge-flaky"' in r.text
    assert "3 failing" in r.text
    assert "auto-disables at 5" in r.text


def test_sources_page_disabled_source_shows_disabled_badge_and_enable_button():
    subs = [{"source_type": "news", "identifier": "x", "name": "Dead Feed", "enabled": False, "consecutive_failures": 5}]
    with _subs(subs):
        r = client.get("/sources")
    assert "disabled — skipped on collect runs" in r.text
    assert "Enable" in r.text
    assert "Disable" not in r.text


def test_sources_page_enabled_source_shows_disable_button():
    subs = [{"source_type": "news", "identifier": "x", "name": "TechCrunch"}]
    with _subs(subs):
        r = client.get("/sources")
    assert "Disable" in r.text


# ---- POST /sources/toggle -------------------------------------------------------


def test_toggle_enable_flips_flag_synchronously_no_job():
    subs = [{"source_type": "news", "identifier": "x", "name": "Dead Feed", "enabled": False, "consecutive_failures": 5}]
    with _subs(subs), \
         patch("panel.app.config.save_subscriptions") as mock_save, \
         patch("panel.app._pathspec_commit", return_value=None):
        r = client.post("/sources/toggle", data={"name": "Dead Feed", "enable": "true"})

    assert "enabled" in r.text
    assert "subscribes on the next collect run" in r.text
    saved = mock_save.call_args[0][0]
    assert saved[0]["enabled"] is True
    assert saved[0]["consecutive_failures"] == 0
    # No async job for the enable path — no MCP call needed.
    assert 'hx-get="/jobs/' not in r.text


def test_toggle_enable_unknown_name_returns_error():
    with _subs([]):
        r = client.post("/sources/toggle", data={"name": "Nonexistent", "enable": "true"})
    assert "not found" in r.text


def test_toggle_disable_enqueues_job(tmp_path):
    from panel import jobs

    subs = [{"source_type": "news", "identifier": "x", "name": "Reddit r/MachineLearning", "consecutive_failures": 5}]
    with _subs(subs), \
         patch("panel.app.mcp_client.is_locked", return_value=False), \
         patch("panel.app._disable_source_job", return_value="Reddit r/MachineLearning"):
        r = client.post("/sources/toggle", data={"name": "Reddit r/MachineLearning", "enable": "false"})
    assert 'hx-get="/jobs/' in r.text
    jobs.JOBS.clear()


def test_toggle_disable_rejects_when_collection_locked():
    subs = [{"source_type": "news", "identifier": "x", "name": "Reddit r/MachineLearning"}]
    with _subs(subs), patch("panel.app.mcp_client.is_locked", return_value=True):
        r = client.post("/sources/toggle", data={"name": "Reddit r/MachineLearning", "enable": "false"})
    assert "Collection is running" in r.text


# ---- disable-source job completion fragment: OOB row patch ---------------------


def test_disable_source_job_result_patches_row_oob():
    from panel import app, jobs

    subs = [{"source_type": "news", "identifier": "x", "name": "Reddit r/MachineLearning", "enabled": False, "consecutive_failures": 5}]
    with _subs(subs):
        jobs.JOBS.clear()
        job_id = "test-disable-job"
        jobs.JOBS[job_id] = jobs.Job(name="disable-source", status="done", result="Reddit r/MachineLearning")
        frag = app._job_fragment(job_id)

    assert "disabled and archived" in frag
    assert 'hx-swap-oob="true"' in frag
    assert 'id="src-row-reddit-r-machinelearning"' in frag
    assert "just-updated" in frag
    jobs.JOBS.clear()
