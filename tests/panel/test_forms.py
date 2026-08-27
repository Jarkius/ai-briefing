"""S5 tests: style/sources/schedule/settings forms — file writes, validation,
pathspec commits (mocked), config.reload() invocation, and no-value-logging.
"""

import os
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient

from panel.app import app

client = TestClient(app)


# ---- style -------------------------------------------------------------------


def test_style_get_shows_current_file(tmp_path):
    style = tmp_path / "newsletter_style.md"
    style.write_text("- be concise")
    with patch("briefing.config.STYLE_PATH", str(style)):
        r = client.get("/style")
    assert "- be concise" in r.text


def test_style_post_writes_file_and_commits(tmp_path):
    style = tmp_path / "newsletter_style.md"
    style.write_text("old")
    with patch("briefing.config.STYLE_PATH", str(style)), \
         patch("panel.app.config.STYLE_PATH", str(style)), \
         patch("panel.app._pathspec_commit", return_value=None) as commit:
        r = client.post("/style", data={"style_text": "new rules"})
    assert style.read_text() == "new rules"
    commit.assert_called_once()
    assert "saved" in r.text


def test_style_commit_failure_is_banner_not_500(tmp_path):
    style = tmp_path / "s.md"
    with patch("panel.app.config.STYLE_PATH", str(style)), \
         patch("panel.app._pathspec_commit", return_value="dirty index"):
        r = client.post("/style", data={"style_text": "x"})
    assert r.status_code == 200
    assert "git commit failed" in r.text
    assert style.read_text() == "x"  # the write itself always succeeds first


# ---- sources -----------------------------------------------------------------


def _subs_env(tmp_path, initial="[]"):
    subs = tmp_path / "subscriptions.json"
    subs.write_text(initial)
    return patch("briefing.config.SUBSCRIPTIONS_PATH", str(subs)), subs


def test_sources_add_validates_source_type(tmp_path):
    ctx, subs = _subs_env(tmp_path)
    with ctx:
        r = client.post("/sources", data={"source_type": "carrier-pigeon", "identifier": "x", "name": "X"})
    assert "unknown source_type" in r.text
    assert subs.read_text() == "[]"


def test_sources_add_requires_name(tmp_path):
    # collector reconciles on (type, name); nameless entries are skipped
    ctx, subs = _subs_env(tmp_path)
    with ctx:
        r = client.post("/sources", data={"source_type": "news", "identifier": "https://x.com/feed", "name": "  "})
    assert "required" in r.text
    assert subs.read_text() == "[]"


def test_sources_add_appends_and_commits(tmp_path):
    import json

    ctx, subs = _subs_env(tmp_path)
    with ctx, patch("panel.app._pathspec_commit", return_value=None) as commit:
        r = client.post("/sources", data={"source_type": "news", "identifier": "https://x.com/feed", "name": "X Blog"})
    data = json.loads(subs.read_text())
    assert data == [{"source_type": "news", "identifier": "https://x.com/feed", "name": "X Blog"}]
    commit.assert_called_once()
    assert "next collect run" in r.text


def test_sources_duplicate_rejected(tmp_path):
    initial = '[{"source_type": "news", "identifier": "u", "name": "X Blog"}]'
    ctx, subs = _subs_env(tmp_path, initial)
    with ctx:
        r = client.post("/sources", data={"source_type": "news", "identifier": "u2", "name": "X Blog"})
    assert "already subscribed" in r.text


# ---- schedule ----------------------------------------------------------------


def test_schedule_rejects_out_of_range():
    with patch("panel.app.sys.platform", "darwin"):
        r = client.post("/schedule", data={"hour": "25", "minute": "0"})
    assert "hour must be" in r.text


def test_schedule_writes_plist_and_reloads(tmp_path):
    import plistlib

    plist_path = tmp_path / "com.user.ai-briefing.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump({"Label": "com.user.ai-briefing",
                       "StartCalendarInterval": {"Hour": 5, "Minute": 0}}, f)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""
        return R()

    with patch("panel.app.PLIST_PATH", str(plist_path)), \
         patch("panel.app.sys.platform", "darwin"), \
         patch("panel.app.subprocess.run", side_effect=fake_run):
        r = client.post("/schedule", data={"hour": "6", "minute": "30"})

    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert ["launchctl", "unload", str(plist_path)] in calls
    assert ["launchctl", "load", str(plist_path)] in calls
    assert "06:30" in r.text


# ---- settings ----------------------------------------------------------------


def test_settings_post_rewrites_env_calls_reload_and_never_logs(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("# comment kept\nGMAIL_ADDRESS=old@x.com\nUNRELATED=stay\n")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload") as reload_mock:
        r = client.post("/settings", data={
            "GMAIL_ADDRESS": "new@x.com",
            "GEMINI_API_KEY": "sk-SECRET-VALUE-123",
        })
    content = env.read_text()
    assert "GMAIL_ADDRESS=new@x.com" in content
    assert "GEMINI_API_KEY=sk-SECRET-VALUE-123" in content
    assert "# comment kept" in content       # unrelated lines preserved
    assert "UNRELATED=stay" in content
    reload_mock.assert_called_once()          # review M2
    if sys.platform != "win32":
        assert oct(os.stat(env).st_mode & 0o777) == "0o600"
    # On Windows, os.stat().st_mode can't reflect real ACLs — see
    # config.restrict_to_owner_only's docstring; test_config.py covers the
    # real icacls-based restriction there.
    # never logged server-side
    captured = capsys.readouterr()
    assert "sk-SECRET-VALUE-123" not in captured.out + captured.err
    assert "sk-SECRET-VALUE-123" not in r.text  # response echoes no secrets


def test_settings_page_masks_secret_inputs():
    r = client.get("/settings")
    assert 'type="password" name="GMAIL_APP_PASSWORD"' in r.text.replace("\n", " ") or \
           'type="password"' in r.text


def test_settings_page_shows_provider_order():
    r = client.get("/settings")
    assert "provider-list" in r.text
    # default order: bedrock first
    import re
    rows = re.findall(r'data-provider="([a-z-]+)"', r.text)
    assert rows[0] == "bedrock"
    assert set(rows) == {"bedrock", "gemini", "maxplus", "claude-cli"}


def test_settings_post_saves_provider_order(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GMAIL_ADDRESS=a@x.com\n")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload") as reload_mock:
        r = client.post("/settings", data={
            "provider_order_0": "claude-cli",
            "provider_order_1": "bedrock",
            "provider_order_2": "gemini",
            "provider_order_3": "maxplus",
        })
    assert "PROVIDER_ORDER=claude-cli,bedrock,gemini,maxplus" in env.read_text()
    reload_mock.assert_called_once()


def test_settings_post_ignores_unknown_provider_names(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload"):
        client.post("/settings", data={
            "provider_order_0": "bedrock",
            "provider_order_1": "carrier-pigeon",
            "provider_order_2": "gemini",
        })
    assert "PROVIDER_ORDER=bedrock,gemini" in env.read_text()


def test_settings_shows_effective_defaults_not_empty(tmp_path, monkeypatch):
    # BEDROCK_MODEL/REGION have config defaults but may be absent from
    # os.environ — the form must show the effective value, never blank.
    monkeypatch.delenv("BEDROCK_MODEL", raising=False)
    monkeypatch.delenv("BEDROCK_REGION", raising=False)
    r = client.get("/settings")
    assert 'name="BEDROCK_MODEL" value=""' not in r.text
    assert "claude-sonnet-5" in r.text  # config default surfaced


def test_settings_claude_cli_model_is_dropdown():
    r = client.get("/settings")
    assert '<select name="CLAUDE_CLI_MODEL">' in r.text
    for m in ("sonnet", "opus", "haiku"):
        assert f'value="{m}"' in r.text


# ---- settings + Bitwarden routing --------------------------------------------


def test_settings_post_writes_to_bitwarden_when_key_has_no_active_env_line(tmp_path):
    """A key that's commented out in .env (Bitwarden-sourced) must not get
    a new active line appended — that would silently override Bitwarden.
    It should route to bws_write_secret() instead."""
    env = tmp_path / ".env"
    env.write_text("#GMAIL_ADDRESS=old-bitwarden-value\n")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload"), \
         patch("panel.app.config.bws_list_secrets",
               return_value=[{"id": "abc-123", "key": "GMAIL_ADDRESS", "value": "old-bitwarden-value"}]), \
         patch("panel.app.config.bws_write_secret", return_value=(True, None)) as write_mock:
        r = client.post("/settings", data={"GMAIL_ADDRESS": "new@x.com"})
    write_mock.assert_called_once_with("GMAIL_ADDRESS", "new@x.com")
    assert "GMAIL_ADDRESS=" not in env.read_text().replace("#GMAIL_ADDRESS=", "")
    assert "Bitwarden" in r.text


def test_settings_post_still_uses_env_for_key_not_in_bitwarden(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload"), \
         patch("panel.app.config.bws_list_secrets", return_value=[]), \
         patch("panel.app.config.bws_write_secret") as write_mock:
        client.post("/settings", data={"GMAIL_ADDRESS": "new@x.com"})
    write_mock.assert_not_called()
    assert "GMAIL_ADDRESS=new@x.com" in env.read_text()


def test_settings_post_active_env_line_wins_over_bitwarden(tmp_path):
    """Precedence must match config.py: an already-active .env line keeps
    being edited in .env, never redirected to Bitwarden, even if the same
    key also exists there."""
    env = tmp_path / ".env"
    env.write_text("GMAIL_ADDRESS=active-local-override@x.com\n")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload"), \
         patch("panel.app.config.bws_list_secrets",
               return_value=[{"id": "abc-123", "key": "GMAIL_ADDRESS", "value": "bitwarden-value"}]), \
         patch("panel.app.config.bws_write_secret") as write_mock:
        client.post("/settings", data={"GMAIL_ADDRESS": "new@x.com"})
    write_mock.assert_not_called()
    assert "GMAIL_ADDRESS=new@x.com" in env.read_text()


def test_settings_post_surfaces_bitwarden_write_failure(tmp_path):
    env = tmp_path / ".env"
    env.write_text("#GMAIL_ADDRESS=old\n")
    with patch("briefing.config.ENV_PATH", str(env)), \
         patch("panel.app.config.ENV_PATH", str(env)), \
         patch("panel.app.config.reload"), \
         patch("panel.app.config.bws_list_secrets",
               return_value=[{"id": "abc-123", "key": "GMAIL_ADDRESS", "value": "old"}]), \
         patch("panel.app.config.bws_write_secret",
               return_value=(False, "permission denied: token is read-only")):
        r = client.post("/settings", data={"GMAIL_ADDRESS": "new@x.com"})
    assert "permission denied" in r.text
    assert "banner-err" in r.text


# ---- Gmail OAuth re-authorize (settings) -------------------------------------


def test_settings_shows_expiring_soon_banner_with_button():
    from unittest.mock import patch

    with patch("panel.app.gmail_api.token_status",
               return_value={"state": "expiring_soon", "age_days": 5.5, "days_left": 1.5}):
        r = client.get("/settings")
    assert "expires in 1.5 day" in r.text
    assert "gmail-reauth-btn" in r.text
    assert 'hx-post="/settings/gmail-reauth"' in r.text


def test_settings_shows_expired_banner():
    from unittest.mock import patch

    with patch("panel.app.gmail_api.token_status",
               return_value={"state": "expired", "age_days": 9.0, "days_left": -2.0}):
        r = client.get("/settings")
    assert "expired" in r.text.lower()
    assert "gmail-reauth-btn" in r.text


def test_settings_no_oauth_banner_when_healthy():
    from unittest.mock import patch

    with patch("panel.app.gmail_api.token_status",
               return_value={"state": "ok", "age_days": 1.0, "days_left": 6.0}):
        r = client.get("/settings")
    assert "gmail-reauth-btn" not in r.text


def test_settings_shows_not_configured_banner():
    from unittest.mock import patch

    with patch("panel.app.gmail_api.token_status",
               return_value={"state": "not_configured", "age_days": None, "days_left": None}):
        r = client.get("/settings")
    assert "isn&#39;t set up" in r.text or "isn't set up" in r.text


def test_gmail_reauth_route_enqueues_job():
    from unittest.mock import patch

    from panel import jobs

    jobs.JOBS.clear()
    with patch("panel.app._oauth_reauth_job", return_value="reauthorized"):
        r = client.post("/settings/gmail-reauth")
    assert 'hx-get="/jobs/' in r.text or "gmail-reauth" in r.text
    jobs.JOBS.clear()


def test_gmail_reauth_double_submit_reattaches():
    from panel import jobs

    jobs.JOBS.clear()
    jobs.JOBS["reauth-busy"] = jobs.Job(name="gmail-reauth")
    try:
        r = client.post("/settings/gmail-reauth")
        assert "reauth-busy" in r.text
        assert len(jobs.JOBS) == 1
    finally:
        jobs.JOBS.clear()


def test_gmail_reauth_job_fragment_success_message():
    from panel import jobs
    from panel.app import _job_fragment

    jobs.JOBS.clear()
    j = jobs.Job(name="gmail-reauth", status="done")
    j.result = "reauthorized"
    jobs.JOBS["done1"] = j
    frag = _job_fragment("done1")
    assert "re-authorized" in frag
    assert "every 2s" not in frag  # terminal, polling stops
    jobs.JOBS.clear()
