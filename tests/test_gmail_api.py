"""Tests for gmail_api.py: the Gmail-API-over-HTTPS fallback used when raw
SMTP/IMAP protocol ports are blocked (see sender.py's fallback chain).

No real network or OAuth flow — google-api-python-client's build() and the
Credentials loader are mocked throughout.
"""

import os
import sys
from unittest.mock import MagicMock, patch

from briefing import gmail_api


def test_is_configured_false_when_token_file_missing(tmp_path):
    with patch.object(gmail_api, "TOKEN_PATH", str(tmp_path / "no_such_token.json")):
        assert gmail_api.is_configured() is False


def test_is_configured_true_when_token_file_present(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)):
        assert gmail_api.is_configured() is True


def test_get_credentials_raises_clear_error_when_unconfigured(tmp_path):
    with patch.object(gmail_api, "TOKEN_PATH", str(tmp_path / "missing.json")):
        try:
            gmail_api._get_credentials()
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "setup_gmail_oauth.py" in str(e)


def test_get_credentials_returns_valid_token_without_refresh(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")

    fake_creds = MagicMock(expired=False, refresh_token="rt")

    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=fake_creds):
        creds = gmail_api._get_credentials()

    assert creds is fake_creds
    fake_creds.refresh.assert_not_called()
    # no refresh means no atomic write: original content untouched, no tmp file left behind
    assert not (tmp_path / "token.json.tmp").exists()
    assert token_path.read_text() == "{}"


def test_get_credentials_refreshes_expired_token(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")

    fake_creds = MagicMock(expired=True, refresh_token="rt")
    fake_creds.to_json.return_value = '{"refreshed": true}'

    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=fake_creds):
        creds = gmail_api._get_credentials()

    assert creds is fake_creds
    fake_creds.refresh.assert_called_once()
    assert token_path.read_text() == '{"refreshed": true}'


def test_get_credentials_renames_token_and_raises_clear_error_on_invalid_grant(tmp_path):
    """RefreshError (invalid_grant) means the refresh_token was revoked —
    no retry recovers it, only a human re-running the browser consent
    flow. The dead token must move aside (never delete — nothing is
    deleted) so is_configured() reflects reality on the very next call."""
    from google.auth.exceptions import RefreshError

    token_path = tmp_path / "token.json"
    token_path.write_text('{"refresh_token": "dead"}')

    fake_creds = MagicMock(expired=True, refresh_token="dead")
    fake_creds.refresh.side_effect = RefreshError(
        "invalid_grant: Token has been expired or revoked."
    )

    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=fake_creds):
        try:
            gmail_api._get_credentials()
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "invalid_grant" in str(e) or "revoked" in str(e)
            assert "setup_gmail_oauth.py" in str(e)

    assert not token_path.exists()
    assert (tmp_path / "token.json.expired").read_text() == '{"refresh_token": "dead"}'
    # Reflects reality on the next call — no more silently-broken retries.
    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)):
        assert gmail_api.is_configured() is False


def test_service_builds_gmail_client_with_credentials():
    fake_creds = MagicMock()
    fake_service = MagicMock()

    with patch.object(gmail_api, "_get_credentials", return_value=fake_creds), \
         patch("googleapiclient.discovery.build", return_value=fake_service) as mock_build:
        service = gmail_api._service()

    mock_build.assert_called_once_with("gmail", "v1", credentials=fake_creds)
    assert service is fake_service


def test_send_email_via_api_builds_and_sends_raw_message():
    fake_service = MagicMock()
    with patch.object(gmail_api, "_service", return_value=fake_service):
        gmail_api.send_email_via_api("subject", "<p>html</p>")

    fake_service.users.return_value.messages.return_value.send.assert_called_once()
    _, kwargs = fake_service.users.return_value.messages.return_value.send.call_args
    assert kwargs["userId"] == "me"
    assert "raw" in kwargs["body"]


def test_already_sent_today_via_api_true_when_messages_found():
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "abc"}]
    }
    with patch.object(gmail_api, "_service", return_value=fake_service):
        assert gmail_api.already_sent_today_via_api("AI Briefing Part 1") is True


def test_already_sent_today_via_api_false_when_no_messages():
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    with patch.object(gmail_api, "_service", return_value=fake_service):
        assert gmail_api.already_sent_today_via_api("AI Briefing Part 1") is False


def test_refresh_write_is_atomic_and_owner_only(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")

    fake_creds = MagicMock(expired=True, refresh_token="rt")
    fake_creds.to_json.return_value = '{"refreshed": true}'

    with patch.object(gmail_api, "TOKEN_PATH", str(token_path)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=fake_creds):
        gmail_api._get_credentials()

    # temp file must be gone (os.replace'd into place), token owner-only
    assert not (tmp_path / "token.json.tmp").exists()
    assert token_path.read_text() == '{"refreshed": true}'
    if sys.platform != "win32":
        assert oct(os.stat(token_path).st_mode & 0o777) == "0o600"
    # On Windows, os.stat().st_mode can never reflect real ACLs (it only
    # tracks the read-only attribute) — see config.restrict_to_owner_only's
    # docstring. test_config.py covers the real icacls-based restriction.
