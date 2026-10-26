"""Tests for the GREEN-API MAX smoke test hardening.

These tests never touch the real GREEN-API: every HTTP call is mocked and the
real ``.env`` file is never read (``dotenv.load_dotenv`` is patched out while
the module is imported).
"""

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SMOKE_TEST_PATH = PROJECT_ROOT / "scripts" / "green_api_smoke_test.py"

# Deterministic configuration used to import the module. The token value is a
# fake placeholder used to assert it never appears in logs or errors.
_ENV = {
    "GREEN_API_BASE_URL": "https://example.green-api.com",
    "GREEN_API_ID_INSTANCE": "1101000001",
    "GREEN_API_TOKEN_INSTANCE": "test-token-value",
    "TEST_PHONE_NUMBER": "79991234567",
    "TEST_MESSAGE": "hello",
    "SMOKE_TEST_TIMEOUT_SECONDS": "180",
}
FAKE_TOKEN = _ENV["GREEN_API_TOKEN_INSTANCE"]


def _load_smoke_module():
    """Import the smoke-test module without reading the real .env file."""
    import dotenv

    saved = {key: os.environ.get(key) for key in _ENV}
    os.environ.update(_ENV)
    try:
        with mock.patch.object(dotenv, "load_dotenv", lambda *args, **kwargs: False):
            spec = importlib.util.spec_from_file_location(
                "green_api_smoke_test", SMOKE_TEST_PATH
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


smoke = _load_smoke_module()


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` (no real network involved)."""

    def __init__(self, status_code=200, json_data=None, text="", headers=None,
                 json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers if headers is not None else {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json available")
        return self._json_data


@contextmanager
def _valid_config(**overrides):
    """Patch the module-level configuration with valid values."""
    config = {
        "GREEN_API_BASE_URL": "https://example.green-api.com",
        "GREEN_API_ID_INSTANCE": "1101000001",
        "GREEN_API_TOKEN_INSTANCE": FAKE_TOKEN,
        "TEST_PHONE_NUMBER": "79991234567",
        "TEST_MESSAGE": "hello",
    }
    config.update(overrides)
    with mock.patch.multiple(smoke, **config):
        yield


def _client_with(fake_http_client):
    client = smoke.GreenApiClient(
        base_url="https://example.green-api.com",
        id_instance="1101000001",
        token=FAKE_TOKEN,
        timeout=smoke.HTTP_TIMEOUT_DEFAULT,
    )
    client._client = fake_http_client
    return client


# ---------------------------------------------------------------------------
# URL masking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method",
    ["checkAccount", "sendMessage", "receiveNotification", "deleteNotification"],
)
def test_sanitize_url_masks_token(method):
    url = f"https://example.green-api.com/waInstance1101000001/{method}/{FAKE_TOKEN}"
    masked = smoke._sanitize_url(url)
    assert FAKE_TOKEN not in masked
    assert masked.endswith(f"/{method}/<hidden>")


def test_sanitize_url_masks_receipt_id_after_token():
    """deleteNotification puts receiptId after the token; both must be hidden."""
    url = (
        "https://example.green-api.com/waInstance1101000001"
        f"/deleteNotification/{FAKE_TOKEN}/1234567"
    )
    masked = smoke._sanitize_url(url)
    assert FAKE_TOKEN not in masked
    assert "1234567" not in masked
    assert masked.endswith("/deleteNotification/<hidden>")


def test_sanitize_url_masks_token_before_query_string():
    url = (
        "https://example.green-api.com/waInstance1101000001"
        f"/receiveNotification/{FAKE_TOKEN}?receiveTimeout=10"
    )
    masked = smoke._sanitize_url(url)
    assert FAKE_TOKEN not in masked


# ---------------------------------------------------------------------------
# Secret-safe error reporting
# ---------------------------------------------------------------------------

def test_safe_error_detail_excludes_exception_string():
    exc = httpx.ConnectError(
        f"failed to connect to /waInstance1101000001/checkAccount/{FAKE_TOKEN}"
    )
    detail = smoke._safe_error_detail("CheckAccount", exc)
    assert FAKE_TOKEN not in detail
    assert "waInstance" not in detail
    assert "CheckAccount" in detail
    assert "ConnectError" in detail


def test_check_account_httpx_error_is_secret_safe():
    fake = mock.Mock()
    fake.post.side_effect = httpx.ConnectError(
        f"failed to connect to /waInstance1101000001/checkAccount/{FAKE_TOKEN}"
    )
    client = _client_with(fake)

    with pytest.raises(smoke.ConfigError) as excinfo:
        client.check_account("79991234567")

    message = str(excinfo.value)
    assert FAKE_TOKEN not in message
    assert "waInstance" not in message
    assert "ConnectError" in message


def test_raise_api_error_does_not_leak_token_or_url():
    resp = _FakeResponse(status_code=400, json_data={"code": 400, "message": "bad"})
    with pytest.raises(smoke.ConfigError) as excinfo:
        smoke._raise_api_error("SendMessage", resp)

    message = str(excinfo.value)
    assert FAKE_TOKEN not in message
    assert "waInstance" not in message
    assert "example.green-api.com" not in message
    assert "400" in message


def test_safe_parse_json_does_not_leak_response_body():
    resp = _FakeResponse(
        status_code=200,
        text="<html>secret response body</html>",
        headers={"content-type": "text/html"},
        json_error=True,
    )
    with pytest.raises(smoke.ConfigError) as excinfo:
        smoke._safe_parse_json(resp, "SendMessage")

    message = str(excinfo.value)
    assert "secret response body" not in message
    assert "200" in message


# ---------------------------------------------------------------------------
# Phone validation (GREEN-API MAX CheckAccount)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phone", ["79991234567", "375291234567"])
def test_validate_config_accepts_valid_phones(phone):
    with _valid_config(TEST_PHONE_NUMBER=phone):
        smoke.validate_config(smoke.MODE_CHECK_ONLY)


@pytest.mark.parametrize(
    "phone",
    [
        "1234567890",          # 10 digits
        "1234567890123456",    # 16 digits (arbitrary 7-15 range)
        "89991234567",         # 11 digits, wrong country code
        "37529123456",         # 11 digits starting with 375
        "775291234567",        # 12 digits, wrong country code
        "+79991234567",        # plus sign
        "7 999 123 45 67",     # spaces
        "7-999-123-45-67",     # separators
        "3752912345678",       # 13 digits
    ],
)
def test_validate_config_rejects_invalid_phones(phone):
    with _valid_config(TEST_PHONE_NUMBER=phone):
        with pytest.raises(smoke.ConfigError):
            smoke.validate_config(smoke.MODE_CHECK_ONLY)


# ---------------------------------------------------------------------------
# Mode-aware configuration validation
# ---------------------------------------------------------------------------

def test_check_only_does_not_require_test_message():
    with _valid_config(TEST_MESSAGE=""):
        smoke.validate_config(smoke.MODE_CHECK_ONLY)


def test_send_mode_requires_non_empty_test_message():
    with _valid_config(TEST_MESSAGE=""):
        with pytest.raises(smoke.ConfigError):
            smoke.validate_config(smoke.MODE_SEND_AND_WAIT_READ)


def test_send_mode_rejects_message_longer_than_limit():
    with _valid_config(TEST_MESSAGE="x" * 4001):
        with pytest.raises(smoke.ConfigError):
            smoke.validate_config(smoke.MODE_SEND_AND_WAIT_READ)


def test_send_mode_accepts_message_at_limit():
    with _valid_config(TEST_MESSAGE="x" * 4000):
        smoke.validate_config(smoke.MODE_SEND_AND_WAIT_READ)


def test_check_only_ignores_message_length():
    with _valid_config(TEST_MESSAGE="x" * 5000):
        smoke.validate_config(smoke.MODE_CHECK_ONLY)


# ---------------------------------------------------------------------------
# ReceiveNotification timeout
# ---------------------------------------------------------------------------

def test_generic_timeout_is_shorter_than_max_long_poll():
    assert smoke.RECEIVE_TIMEOUT_MAX == 60
    assert smoke.HTTP_TIMEOUT_DEFAULT < smoke.RECEIVE_TIMEOUT_MAX


@pytest.mark.parametrize("receive_timeout", [5, 10, 60])
def test_receive_request_timeout_exceeds_receive_timeout(receive_timeout):
    client = _client_with(mock.Mock())
    assert client._receive_request_timeout(receive_timeout) > receive_timeout


def test_long_poll_is_not_cut_off_by_generic_timeout():
    fake = mock.Mock()
    fake.get.return_value = _FakeResponse(status_code=200, json_data=[])
    client = _client_with(fake)

    client.receive_notification(receive_timeout=60)

    _, kwargs = fake.get.call_args
    assert kwargs["timeout"] > 60
    assert kwargs["timeout"] > smoke.HTTP_TIMEOUT_DEFAULT


def test_receive_timeout_out_of_range_falls_back_to_default():
    fake = mock.Mock()
    fake.get.return_value = _FakeResponse(status_code=200, json_data=[])
    client = _client_with(fake)

    client.receive_notification(receive_timeout=600)

    _, kwargs = fake.get.call_args
    assert kwargs["params"]["receiveTimeout"] == smoke.RECEIVE_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# DeleteNotification token safety
# ---------------------------------------------------------------------------

def test_delete_notification_hides_token_and_receipt_in_logs(capsys):
    fake = mock.Mock()
    fake.delete.return_value = _FakeResponse(status_code=200, json_data={"result": True})
    client = _client_with(fake)

    assert client.delete_notification(12345) is True

    called_url = fake.delete.call_args.args[0]
    assert called_url.endswith(f"/deleteNotification/{FAKE_TOKEN}/12345")

    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out
    assert "12345" not in captured.out


# ---------------------------------------------------------------------------
# Send guard rails
# ---------------------------------------------------------------------------

def test_confirm_send_requires_exact_send(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *args: "yes")
    assert smoke._confirm_send("1000", "hi") is False


def test_confirm_send_accepts_send(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *args: "SEND")
    assert smoke._confirm_send("1000", "hi") is True


def test_parse_args_requires_a_mode():
    with pytest.raises(SystemExit):
        smoke.parse_args([])


def test_parse_args_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        smoke.parse_args(["--check-only", "--send-and-wait-read"])


def test_parse_args_accepts_check_only():
    assert smoke.parse_args(["--check-only"]).check_only is True


def test_check_only_never_sends(monkeypatch):
    with _valid_config(TEST_MESSAGE=""):
        fake_client = mock.Mock()
        fake_client.check_account.return_value = smoke.CheckAccountResult(
            exist=True, chat_id="1000", from_cache=True, raw_response={"exist": True}
        )
        monkeypatch.setattr(smoke, "_build_client", lambda: fake_client)
        result = smoke.run_check_only()

    assert result == 0
    fake_client.send_message.assert_not_called()


def test_send_flow_does_not_send_without_confirmation(monkeypatch):
    with _valid_config():
        fake_client = mock.Mock()
        fake_client.check_account.return_value = smoke.CheckAccountResult(
            exist=True, chat_id="1000", from_cache=True, raw_response={"exist": True}
        )
        monkeypatch.setattr(smoke, "_build_client", lambda: fake_client)
        monkeypatch.setattr(smoke, "_confirm_send", lambda chat_id, message: False)
        result = smoke.run_send_and_wait_read()

    assert result == 1
    fake_client.send_message.assert_not_called()
