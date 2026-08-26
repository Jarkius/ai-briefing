"""Tests for config.py: .env parsing, env-driven module binding, the
required-config guard, and the subscriptions/style file loaders.
"""

import copy
import json
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from briefing import config, gmail_api

# _bind() rebinds these as module globals from os.environ; snapshot/restore
# them around every test so a test that calls _bind() (or patches one
# directly) can't leak state into other test files that read config.X.
_BIND_ATTRS = [
    "MAXPLUS_API_KEY", "MAXPLUS_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "CLAUDE_CLI_ENABLED", "CLAUDE_CLI_MODEL",
    "BEDROCK_ENABLED", "BEDROCK_MODEL", "BEDROCK_REGION",
    "PROVIDER_ORDER",
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GMAIL_OAUTH_PUBLISHED",
    "RECIPIENT_EMAIL", "RECIPIENT_EMAILS",
    "REQUIRED_ENV",
]

# Every os.environ key _bind() reads; cleared before each _bind() test so
# whatever real .env/shell state exists on this machine can't leak in.
_ENV_KEYS = [
    "MAXPLUS_API_KEY", "MAXPLUS_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "CLAUDE_CLI_ENABLED", "CLAUDE_CLI_MODEL",
    "BEDROCK_ENABLED", "BEDROCK_MODEL", "BEDROCK_REGION", "AWS_REGION",
    "PROVIDER_ORDER",
    "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GMAIL_OAUTH_PUBLISHED", "RECIPIENT_EMAIL",
]


@pytest.fixture(autouse=True)
def _restore_config_bindings():
    snapshot = {name: copy.deepcopy(getattr(config, name)) for name in _BIND_ATTRS}
    yield
    for name, value in snapshot.items():
        setattr(config, name, value)


def _clear_bind_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---- _load_env --------------------------------------------------------------


def test_load_env_noop_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "no_such.env"))
    config._load_env()  # must not raise


def test_load_env_sets_variable(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_KEY=hello\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "hello"


def test_load_env_skips_blank_and_comment_lines(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("\n# a comment\nMY_TEST_KEY=value\n\n# MY_TEST_KEY2=nope\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    monkeypatch.delenv("MY_TEST_KEY2", raising=False)

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "value"
    assert "MY_TEST_KEY2" not in os.environ


def test_load_env_strips_inline_comment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_KEY=value   # trailing note\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "value"


def test_load_env_strips_double_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('MY_TEST_KEY="quoted value"\n')
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "quoted value"


def test_load_env_strips_single_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_KEY='quoted value'\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "quoted value"


def test_load_env_overrides_ambient_env_var(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_KEY=from_dotenv\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.setenv("MY_TEST_KEY", "from_shell")

    config._load_env()

    assert os.environ["MY_TEST_KEY"] == "from_dotenv"


# ---- _load_bitwarden ----------------------------------------------------


def test_load_bitwarden_noop_when_token_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(tmp_path / "no_such_token"))
    with patch("subprocess.run") as mock_run:
        config._load_bitwarden()
    mock_run.assert_not_called()


def test_load_bitwarden_noop_when_bws_binary_missing(tmp_path, monkeypatch):
    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    with patch("shutil.which", return_value=None), \
         patch("os.path.exists", side_effect=lambda p: p == str(token_path)), \
         patch("subprocess.run") as mock_run:
        config._load_bitwarden()
    mock_run.assert_not_called()


def test_load_bitwarden_uses_local_bin_fallback_when_which_fails(tmp_path, monkeypatch):
    """No Homebrew formula for bws exists (see README) — shutil.which()
    misses it if the user only followed the documented ~/.local/bin
    install path and that directory isn't on PATH for this process."""
    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    fallback = os.path.expanduser("~/.local/bin/bws")
    fake_result = MagicMock(stdout="")
    with patch("shutil.which", return_value=None), \
         patch("os.path.exists", side_effect=lambda p: p in (str(token_path), fallback)), \
         patch("subprocess.run", return_value=fake_result) as mock_run:
        config._load_bitwarden()
    args, _ = mock_run.call_args
    assert args[0][0] == fallback


def test_load_bitwarden_applies_env_output(tmp_path, monkeypatch):
    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    fake_result = MagicMock(stdout='MY_TEST_KEY="from_bitwarden"\n')
    with patch("shutil.which", return_value="/usr/local/bin/bws"), \
         patch("subprocess.run", return_value=fake_result) as mock_run:
        config._load_bitwarden()
    assert os.environ["MY_TEST_KEY"] == "from_bitwarden"
    args, kwargs = mock_run.call_args
    assert args[0] == ["/usr/local/bin/bws", "secret", "list", config.BWS_PROJECT_ID, "-o", "env"]
    assert kwargs["env"]["BWS_ACCESS_TOKEN"] == "fake-token"


def test_load_bitwarden_survives_subprocess_failure(tmp_path, monkeypatch):
    import subprocess

    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    with patch("shutil.which", return_value="/usr/local/bin/bws"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bws", timeout=15)):
        config._load_bitwarden()  # must not raise


def test_load_env_lets_dotenv_override_bitwarden(tmp_path, monkeypatch):
    """Precedence: Bitwarden fills os.environ first, then .env's existing
    always-wins behavior applies on top — commenting a key out in .env
    means "use Bitwarden's value", not "use nothing"."""
    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    env_file = tmp_path / ".env"
    env_file.write_text("MY_TEST_KEY=from_dotenv\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    fake_result = MagicMock(stdout='MY_TEST_KEY="from_bitwarden"\n')

    with patch("shutil.which", return_value="/usr/local/bin/bws"), \
         patch("subprocess.run", return_value=fake_result):
        config._load_env()

    assert os.environ["MY_TEST_KEY"] == "from_dotenv"


def test_load_env_uses_bitwarden_value_when_dotenv_key_absent(tmp_path, monkeypatch):
    token_path = tmp_path / "bws_access_token"
    token_path.write_text("fake-token\n")
    monkeypatch.setattr(config, "BWS_TOKEN_PATH", str(token_path))
    env_file = tmp_path / ".env"
    env_file.write_text("# MY_TEST_KEY commented out, use Bitwarden instead\n")
    monkeypatch.setattr(config, "ENV_PATH", str(env_file))
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    fake_result = MagicMock(stdout='MY_TEST_KEY="from_bitwarden"\n')

    with patch("shutil.which", return_value="/usr/local/bin/bws"), \
         patch("subprocess.run", return_value=fake_result):
        config._load_env()

    assert os.environ["MY_TEST_KEY"] == "from_bitwarden"


# ---- _bind --------------------------------------------------------------


def test_bind_defaults_when_nothing_set(monkeypatch):
    _clear_bind_env(monkeypatch)

    config._bind()

    assert config.MAXPLUS_API_KEY == ""
    assert config.MAXPLUS_MODEL == "gpt-5.5"
    assert config.GEMINI_API_KEY == ""
    assert config.GEMINI_MODEL == "gemini-flash-latest"
    assert config.CLAUDE_CLI_ENABLED is True
    assert config.CLAUDE_CLI_MODEL == "sonnet"
    assert config.BEDROCK_ENABLED is True
    assert config.BEDROCK_REGION == "ap-southeast-1"
    assert config.PROVIDER_ORDER == ["bedrock", "gemini", "maxplus", "claude-cli"]
    assert config.GMAIL_ADDRESS == ""
    assert config.GMAIL_APP_PASSWORD == ""
    assert config.RECIPIENT_EMAIL == ""
    assert config.RECIPIENT_EMAILS == []
    assert config.GMAIL_OAUTH_PUBLISHED is False
    assert config.REQUIRED_ENV == {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", "No"])
def test_bind_claude_cli_enabled_false_for_falsy_values(monkeypatch, value):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CLI_ENABLED", value)

    config._bind()

    assert config.CLAUDE_CLI_ENABLED is False


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", "No"])
def test_bind_gmail_oauth_published_false_for_falsy_values(monkeypatch, value):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("GMAIL_OAUTH_PUBLISHED", value)

    config._bind()

    assert config.GMAIL_OAUTH_PUBLISHED is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything-else"])
def test_bind_gmail_oauth_published_true_for_truthy_values(monkeypatch, value):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("GMAIL_OAUTH_PUBLISHED", value)

    config._bind()

    assert config.GMAIL_OAUTH_PUBLISHED is True


def test_bind_provider_order_trims_and_lowercases_custom_list(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("PROVIDER_ORDER", " Gemini , MAXPLUS ,bedrock ")

    config._bind()

    assert config.PROVIDER_ORDER == ["gemini", "maxplus", "bedrock"]


def test_bind_recipient_email_comma_list_parses_into_recipient_emails(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("RECIPIENT_EMAIL", "a@example.com, b@example.com ,c@example.com")

    config._bind()

    assert config.RECIPIENT_EMAILS == ["a@example.com", "b@example.com", "c@example.com"]


def test_bind_recipient_email_defaults_to_gmail_address(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")

    config._bind()

    assert config.RECIPIENT_EMAIL == "me@example.com"
    assert config.RECIPIENT_EMAILS == ["me@example.com"]


def test_bind_bedrock_region_falls_back_to_aws_region(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    config._bind()

    assert config.BEDROCK_REGION == "us-east-1"


def test_bind_bedrock_region_explicit_wins_over_aws_region(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_REGION", "eu-west-1")

    config._bind()

    assert config.BEDROCK_REGION == "eu-west-1"


# ---- reload --------------------------------------------------------------


def test_reload_calls_load_env_then_bind(monkeypatch):
    mock_load_env = MagicMock()
    mock_bind = MagicMock()
    monkeypatch.setattr(config, "_load_env", mock_load_env)
    monkeypatch.setattr(config, "_bind", mock_bind)

    config.reload()

    mock_load_env.assert_called_once()
    mock_bind.assert_called_once()


# ---- require_env --------------------------------------------------------------


def test_require_env_passes_when_all_required_present(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "secret"})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", False)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", ["me@example.com"])

    config.require_env()  # must not raise


def test_require_env_exits_when_gmail_missing_and_gmail_api_not_configured(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", False)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", ["me@example.com"])
    monkeypatch.setattr(gmail_api, "is_configured", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        config.require_env()

    assert "GMAIL_ADDRESS" in excinfo.value.code
    assert "GMAIL_APP_PASSWORD" in excinfo.value.code


def test_require_env_forgives_missing_app_password_when_gmail_api_configured(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": ""})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", False)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", ["me@example.com"])
    monkeypatch.setattr(gmail_api, "is_configured", lambda: True)

    config.require_env()  # must not raise


def test_require_env_exits_when_no_api_keys_and_no_claude_cli(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "secret"})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", True)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", ["me@example.com"])
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(SystemExit) as excinfo:
        config.require_env()

    assert "MAXPLUS_API_KEY or GEMINI_API_KEY" in excinfo.value.code


def test_require_env_passes_when_no_api_keys_but_claude_cli_available(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "secret"})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", True)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", ["me@example.com"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/claude")

    config.require_env()  # must not raise


def test_require_env_exits_when_recipient_emails_empty(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_ENV", {"GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "secret"})
    monkeypatch.setattr(config, "MAXPLUS_API_KEY", "key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CLI_ENABLED", False)
    monkeypatch.setattr(config, "RECIPIENT_EMAILS", [])

    with pytest.raises(SystemExit) as excinfo:
        config.require_env()

    assert "RECIPIENT_EMAIL" in excinfo.value.code


# ---- load_subscriptions --------------------------------------------------------------


def test_load_subscriptions_returns_empty_list_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_PATH", str(tmp_path / "no_such.json"))

    assert config.load_subscriptions() == []


def test_load_subscriptions_reads_and_parses_json(tmp_path, monkeypatch):
    subs_path = tmp_path / "subscriptions.json"
    subs_path.write_text(json.dumps([{"source_type": "news", "name": "foo"}]))
    monkeypatch.setattr(config, "SUBSCRIPTIONS_PATH", str(subs_path))

    assert config.load_subscriptions() == [{"source_type": "news", "name": "foo"}]


def test_save_subscriptions_writes_json_with_trailing_newline(tmp_path, monkeypatch):
    subs_path = tmp_path / "subscriptions.json"
    monkeypatch.setattr(config, "SUBSCRIPTIONS_PATH", str(subs_path))

    config.save_subscriptions([{"source_type": "news", "name": "foo"}])

    assert json.loads(subs_path.read_text()) == [{"source_type": "news", "name": "foo"}]
    assert subs_path.read_text().endswith("\n")


# ---- load_style --------------------------------------------------------------


def test_load_style_returns_empty_string_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STYLE_PATH", str(tmp_path / "no_such.md"))

    assert config.load_style() == ""


def test_load_style_reads_file_content(tmp_path, monkeypatch):
    style_path = tmp_path / "style.md"
    style_path.write_text("Be concise.")
    monkeypatch.setattr(config, "STYLE_PATH", str(style_path))

    assert config.load_style() == "Be concise."


# ---- restrict_to_owner_only ----------------------------------------------------

# os.chmod(0o600) is a no-op for access control on Windows/NTFS (it only
# toggles the read-only attribute — os.stat().st_mode reports 0o666 right
# back even after a real icacls-based restriction, confirmed manually).
# These tests exercise both platform branches directly via a mocked
# sys.platform, rather than relying on os.stat() to prove anything on
# Windows.


def test_restrict_to_owner_only_uses_chmod_on_posix(tmp_path, monkeypatch):
    target = tmp_path / "secret.json"
    target.write_text("{}")
    monkeypatch.setattr(config.sys, "platform", "linux")

    with patch("os.chmod") as mock_chmod:
        config.restrict_to_owner_only(str(target))

    mock_chmod.assert_called_once_with(str(target), 0o600)


def test_restrict_to_owner_only_uses_icacls_on_windows(tmp_path, monkeypatch):
    target = tmp_path / "secret.json"
    target.write_text("{}")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("USERDOMAIN", "ATRAPA")
    monkeypatch.setenv("USERNAME", "jsanitareephon")

    with patch("subprocess.run") as mock_run:
        config.restrict_to_owner_only(str(target))

    mock_run.assert_called_once()
    (args,), kwargs = mock_run.call_args
    assert args == [
        "icacls", str(target), "/inheritance:r", "/grant:r", "ATRAPA\\jsanitareephon:F",
    ]
    assert kwargs["check"] is True


def test_restrict_to_owner_only_falls_back_to_username_without_domain(tmp_path, monkeypatch):
    target = tmp_path / "secret.json"
    target.write_text("{}")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setenv("USERNAME", "jsanitareephon")

    with patch("subprocess.run") as mock_run:
        config.restrict_to_owner_only(str(target))

    (args,), _ = mock_run.call_args
    assert args[-1] == "jsanitareephon:F"


def test_restrict_to_owner_only_warns_but_does_not_raise_when_icacls_fails(tmp_path, monkeypatch, capsys):
    target = tmp_path / "secret.json"
    target.write_text("{}")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("USERDOMAIN", "ATRAPA")
    monkeypatch.setenv("USERNAME", "jsanitareephon")

    with patch("subprocess.run", side_effect=OSError("icacls not found")):
        config.restrict_to_owner_only(str(target))  # must not raise

    assert "WARNING" in capsys.readouterr().out
