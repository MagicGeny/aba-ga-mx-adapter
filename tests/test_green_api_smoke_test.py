"""Unit tests for the GREEN-API MAX smoke test script.

All tests use mocked HTTP responses (respx) — no real network calls.
"""

import json
from unittest import mock

import httpx
import pytest
import respx

import green_api_smoke_test as gst


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_base():
    """Return (base_url, id_instance, token) for a fake GREEN-API endpoint."""
    return "https://example.green-api.com", "3100000000", "test_token"


@pytest.fixture
def api_url(mock_base):
    """Return a URL builder for the fake GREEN-API endpoint."""
    base, id_inst, token = mock_base

    def _url(method, suffix=""):
        u = f"{base}/waInstance{id_inst}/{method}/{token}"
        if suffix:
            u += f"/{suffix}"
        return u

    return _url


@pytest.fixture
def client(mock_base):
    """GreenApiClient pointed at the fake endpoint."""
    base, id_inst, token = mock_base
    return gst.GreenApiClient(base_url=base, id_instance=id_inst, token=token)


@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    """Set all config module vars to valid test values."""
    monkeypatch.setattr(gst, "GREEN_API_BASE_URL", "https://example.green-api.com")
    monkeypatch.setattr(gst, "GREEN_API_ID_INSTANCE", "3100000000")
    monkeypatch.setattr(gst, "GREEN_API_TOKEN_INSTANCE", "test_token")
    monkeypatch.setattr(gst, "TEST_PHONE_NUMBER", "79991234567")
    monkeypatch.setattr(gst, "TEST_MESSAGE", "Hello MAX")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


class TestNormalizeBaseUrl:
    def test_strips_trailing_slash(self):
        assert gst._normalize_base_url("https://example.com/") == "https://example.com"

    def test_strips_multiple(self):
        assert gst._normalize_base_url("https://example.com//") == "https://example.com"

    def test_no_change(self):
        assert gst._normalize_base_url("https://example.com") == "https://example.com"

    def test_empty(self):
        assert gst._normalize_base_url("") == ""


class TestSanitizeUrl:
    def test_masks_token(self):
        url = "https://example.com/waInstance123/checkAccount/secret_token"
        sanitized = gst._sanitize_url(url)
        assert "secret_token" not in sanitized
        assert sanitized.endswith("***")

    def test_short_url_is_masked(self):
        # A sanitizer masks the trailing segment of any URL —
        # safer than guessing whether it is a token.
        sanitized = gst._sanitize_url("https://example.com")
        assert "example.com" not in sanitized
        assert sanitized.endswith("***")


# ---------------------------------------------------------------------------
# Phone regex
# ---------------------------------------------------------------------------


class TestPhoneRegex:
    @pytest.mark.parametrize(
        "phone", ["79991234567", "375291234567", "1234567890", "1234567"]
    )
    def test_valid_phones(self, phone):
        assert gst.PHONE_REGEX.match(phone)

    @pytest.mark.parametrize(
        "phone",
        ["+79991234567", "abc", "123", "", "7999-123-45-67", " 79991234567"],
    )
    def test_invalid_phones(self, phone):
        assert not gst.PHONE_REGEX.match(phone)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_config(self):
        gst.validate_config()  # must not raise

    def test_missing_base_url(self, monkeypatch):
        monkeypatch.setattr(gst, "GREEN_API_BASE_URL", "")
        with pytest.raises(gst.ConfigError, match="Missing required configuration"):
            gst.validate_config()

    def test_missing_token(self, monkeypatch):
        monkeypatch.setattr(gst, "GREEN_API_TOKEN_INSTANCE", "")
        with pytest.raises(gst.ConfigError, match="Missing required configuration"):
            gst.validate_config()

    def test_invalid_phone(self, monkeypatch):
        monkeypatch.setattr(gst, "TEST_PHONE_NUMBER", "+79991234567")
        with pytest.raises(gst.ConfigError, match="invalid"):
            gst.validate_config()

    def test_non_https_url(self, monkeypatch):
        monkeypatch.setattr(gst, "GREEN_API_BASE_URL", "http://insecure.example.com")
        with pytest.raises(gst.ConfigError, match="https"):
            gst.validate_config()


# ---------------------------------------------------------------------------
# CheckAccount
# ---------------------------------------------------------------------------


class TestCheckAccount:
    @respx.mock
    def test_exist_true_with_chat(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                200,
                json={"exist": True, "chatId": "10000000", "fromCache": False},
            )
        )
        result = client.check_account("79991234567")
        assert result.exist is True
        assert result.chat_id == "10000000"
        assert result.from_cache is False
        assert result.has_valid_chat is True

    @respx.mock
    def test_exist_false(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                200, json={"exist": False, "chatId": "", "fromCache": False}
            )
        )
        result = client.check_account("79991234567")
        assert result.exist is False
        assert result.has_valid_chat is False

    @respx.mock
    def test_instance_level_error(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                200, json={"status": False, "reason": "Instance is starting"}
            )
        )
        with pytest.raises(gst.ConfigError, match="instance-level error"):
            client.check_account("79991234567")

    @respx.mock
    def test_missing_exist_field(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(200, json={"some": "other"})
        )
        with pytest.raises(gst.ConfigError, match="missing 'exist'"):
            client.check_account("79991234567")

    @respx.mock
    def test_http_error(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                400, json={"error_message": "Validation failed"}
            )
        )
        with pytest.raises(gst.ConfigError, match="HTTP 400"):
            client.check_account("79991234567")

    @respx.mock
    def test_sends_phone_number_as_integer(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                200,
                json={"exist": True, "chatId": "10000000", "fromCache": False},
            )
        )
        client.check_account("79991234567")
        body = json.loads(respx.calls[0].request.content)
        assert body["phoneNumber"] == 79991234567
        assert isinstance(body["phoneNumber"], int)

    @respx.mock
    def test_force_true(self, client, api_url):
        respx.post(api_url("checkAccount")).mock(
            return_value=httpx.Response(
                200,
                json={"exist": True, "chatId": "10000000", "fromCache": False},
            )
        )
        client.check_account("79991234567", force=True)
        body = json.loads(respx.calls[0].request.content)
        assert body["force"] is True


# ---------------------------------------------------------------------------
# SendMessage
# ---------------------------------------------------------------------------


class TestSendMessage:
    @respx.mock
    def test_success(self, client, api_url):
        respx.post(api_url("sendMessage")).mock(
            return_value=httpx.Response(200, json={"idMessage": "msg12345"})
        )
        result = client.send_message("10000000", "Hello")
        assert result.id_message == "msg12345"

    @respx.mock
    def test_missing_id_message(self, client, api_url):
        respx.post(api_url("sendMessage")).mock(
            return_value=httpx.Response(200, json={"some": "response"})
        )
        with pytest.raises(gst.ConfigError, match="missing 'idMessage'"):
            client.send_message("10000000", "Hello")

    @respx.mock
    def test_http_error(self, client, api_url):
        respx.post(api_url("sendMessage")).mock(
            return_value=httpx.Response(
                400, json={"error_message": "Validation failed"}
            )
        )
        with pytest.raises(gst.ConfigError, match="HTTP 400"):
            client.send_message("10000000", "Hello")

    @respx.mock
    def test_sends_correct_body(self, client, api_url):
        respx.post(api_url("sendMessage")).mock(
            return_value=httpx.Response(200, json={"idMessage": "msg123"})
        )
        client.send_message("chat123", "Test message")
        body = json.loads(respx.calls[0].request.content)
        assert body["chatId"] == "chat123"
        assert body["message"] == "Test message"


# ---------------------------------------------------------------------------
# ReceiveNotification
# ---------------------------------------------------------------------------


class TestReceiveNotification:
    @respx.mock
    def test_none_on_empty(self, client, api_url):
        respx.get(api_url("receiveNotification")).mock(
            return_value=httpx.Response(200, json={})
        )
        assert client.receive_notification() is None

    @respx.mock
    def test_dict_wrapped_in_list(self, client, api_url):
        notif = {"receiptId": 123, "body": {"typeWebhook": "outgoingMessageStatus"}}
        respx.get(api_url("receiveNotification")).mock(
            return_value=httpx.Response(200, json=notif)
        )
        result = client.receive_notification()
        assert result is not None
        assert len(result) == 1
        assert result[0]["receiptId"] == 123

    @respx.mock
    def test_returns_list_directly(self, client, api_url):
        notifs = [
            {"receiptId": 1, "body": {"typeWebhook": "outgoingMessageStatus"}},
            {"receiptId": 2, "body": {"typeWebhook": "outgoingMessageStatus"}},
        ]
        respx.get(api_url("receiveNotification")).mock(
            return_value=httpx.Response(200, json=notifs)
        )
        result = client.receive_notification()
        assert result is not None
        assert len(result) == 2

    @respx.mock
    def test_sends_receive_timeout_param(self, client, api_url):
        respx.get(api_url("receiveNotification")).mock(
            return_value=httpx.Response(200, json={})
        )
        client.receive_notification(receive_timeout=25)
        request = respx.calls[0].request
        assert "receiveTimeout=25" in str(request.url)

    @respx.mock
    def test_out_of_range_timeout_falls_back(self, client, api_url):
        respx.get(api_url("receiveNotification")).mock(
            return_value=httpx.Response(200, json={})
        )
        client.receive_notification(receive_timeout=999)
        request = respx.calls[0].request
        assert f"receiveTimeout={gst.RECEIVE_TIMEOUT_DEFAULT}" in str(request.url)


# ---------------------------------------------------------------------------
# DeleteNotification
# ---------------------------------------------------------------------------


class TestDeleteNotification:
    @respx.mock
    def test_success(self, client, api_url):
        respx.delete(api_url("deleteNotification", "999")).mock(
            return_value=httpx.Response(200, json={"result": True})
        )
        success, reason = client.delete_notification(999)
        assert success is True
        assert reason == ""

    @respx.mock
    def test_result_false_with_reason(self, client, api_url):
        respx.delete(api_url("deleteNotification", "999")).mock(
            return_value=httpx.Response(
                200, json={"result": False, "reason": "not found"}
            )
        )
        success, reason = client.delete_notification(999)
        assert success is False
        assert "not found" in reason

    @respx.mock
    def test_includes_receipt_id_in_url(self, client, api_url):
        respx.delete(api_url("deleteNotification", "999")).mock(
            return_value=httpx.Response(200, json={"result": True})
        )
        client.delete_notification(999)
        request = respx.calls[0].request
        assert str(request.url).endswith("/999")


# ---------------------------------------------------------------------------
# Notification model
# ---------------------------------------------------------------------------


class TestNotification:
    def test_from_raw_with_body(self):
        raw = {
            "receiptId": 1234567,
            "body": {
                "typeWebhook": "outgoingMessageStatus",
                "idMessage": "msg123",
                "status": "read",
                "chatId": "10000000",
                "description": "Message read",
                "timestamp": 1755591519,
            },
        }
        n = gst.Notification.from_raw(raw)
        assert n.receipt_id == 1234567
        assert n.type_webhook == "outgoingMessageStatus"
        assert n.id_message == "msg123"
        assert n.status == "read"
        assert n.chat_id == "10000000"
        assert n.description == "Message read"
        assert n.timestamp == 1755591519

    def test_from_raw_without_body(self):
        n = gst.Notification.from_raw({"receiptId": 999})
        assert n.receipt_id == 999
        assert n.type_webhook is None
        assert n.id_message is None
        assert n.status is None

    def test_from_raw_body_not_dict(self):
        n = gst.Notification.from_raw({"receiptId": 1, "body": None})
        assert n.type_webhook is None
        assert n.raw_body == {}

    def _make(
        self, type_webhook="outgoingMessageStatus", id_message="msg123",
        status="read",
    ):
        return gst.Notification(
            type_webhook=type_webhook, id_message=id_message,
            status=status, receipt_id=1, chat_id="c",
            description="d", timestamp=0, raw_body={},
        )

    def test_is_status_notification(self):
        assert self._make().is_status_notification is True

    def test_is_not_status_notification(self):
        assert self._make(type_webhook="incomingMessage").is_status_notification is False

    def test_is_for_message_matching(self):
        assert self._make().is_for_message("msg123") is True

    def test_is_for_message_different_id(self):
        assert self._make(id_message="other").is_for_message("msg123") is False

    def test_is_for_message_non_status_type(self):
        n = self._make(type_webhook="incomingMessage")
        assert n.is_for_message("msg123") is False

    def test_is_unrelated_different_message(self):
        assert self._make(id_message="other").is_unrelated("msg123") is True

    def test_is_unrelated_incoming(self):
        n = self._make(type_webhook="incomingMessage")
        assert n.is_unrelated("msg123") is True

    def test_not_unrelated_matching(self):
        assert self._make().is_unrelated("msg123") is False


# ---------------------------------------------------------------------------
# Orchestration — full sequence with mock client (no HTTP)
# ---------------------------------------------------------------------------


class TestRunSendAndWaitRead:
    """Test orchestration logic with a mock GreenApiClient."""

    def _make_client(
        self, check_result=None, send_result=None,
        receive_sequence=None, delete_result=(True, ""),
    ):
        client = mock.MagicMock(spec=gst.GreenApiClient)
        if check_result is not None:
            client.check_account.return_value = check_result
        if send_result is not None:
            client.send_message.return_value = send_result
        if receive_sequence is not None:
            client.receive_notification.side_effect = receive_sequence
        client.delete_notification.return_value = delete_result
        return client

    def _ok_check(self):
        return gst.CheckAccountResult(
            exist=True, chat_id="10000000", from_cache=False, raw_response={}
        )

    def _ok_send(self):
        return gst.SendMessageResult(id_message="msg123", raw_response={})

    def _status_notif(self, receipt, id_message="msg123", status="read"):
        return [{
            "receiptId": receipt,
            "body": {
                "typeWebhook": "outgoingMessageStatus",
                "idMessage": id_message,
                "status": status,
            },
        }]

    def test_success_delivered_then_read(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        client = self._make_client(
            check_result=self._ok_check(), send_result=self._ok_send(),
            receive_sequence=[
                self._status_notif(1, status="delivered"),
                self._status_notif(2, status="read"),
            ],
        )
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 0
        client.delete_notification.assert_any_call(1)
        client.delete_notification.assert_any_call(2)

    def test_success_direct_read(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        client = self._make_client(
            check_result=self._ok_check(), send_result=self._ok_send(),
            receive_sequence=[self._status_notif(1, status="read")],
        )
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 0

    def test_check_account_fail(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        client = mock.MagicMock(spec=gst.GreenApiClient)
        client.check_account.side_effect = gst.ConfigError("check failed")
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 1
        client.send_message.assert_not_called()

    def test_no_account(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        no_acct = gst.CheckAccountResult(
            exist=False, chat_id=None, from_cache=None, raw_response={}
        )
        client = self._make_client(check_result=no_acct)
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 1
        client.send_message.assert_not_called()

    def test_unrelated_notification_stops(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        unrelated = [{
            "receiptId": 1,
            "body": {"typeWebhook": "incomingMessage", "idMessage": "x"},
        }]
        client = self._make_client(
            check_result=self._ok_check(), send_result=self._ok_send(),
            receive_sequence=[unrelated],
        )
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 1
        client.delete_notification.assert_not_called()

    def test_terminal_failure_status(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        client = self._make_client(
            check_result=self._ok_check(), send_result=self._ok_send(),
            receive_sequence=[self._status_notif(1, status="failed")],
        )
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=60
        )
        assert rc == 1

    def test_timeout_zero_returns_1(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SEND")
        client = self._make_client(
            check_result=self._ok_check(), send_result=self._ok_send(),
            receive_sequence=[self._status_notif(1)],
        )
        rc = gst.run_send_and_wait_read(
            client, "79991234567", "Hello", timeout_seconds=0
        )
        assert rc == 1

    def test_operator_cancel(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "NO")
        client = self._make_client(check_result=self._ok_check())
        with pytest.raises(SystemExit):
            gst.run_send_and_wait_read(
                client, "79991234567", "Hello", timeout_seconds=60
            )
        client.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# main() / CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_check_only(self, monkeypatch):
        client = mock.MagicMock(spec=gst.GreenApiClient)
        client.check_account.return_value = gst.CheckAccountResult(
            exist=True, chat_id="10000000", from_cache=False, raw_response={}
        )
        monkeypatch.setattr(gst, "GreenApiClient", lambda **kw: client)
        rc = gst.main(["--check-only"])
        assert rc == 0
        client.check_account.assert_called_once()
        client.send_message.assert_not_called()
        client.close.assert_called_once()

    def test_requires_mode(self):
        with pytest.raises(SystemExit):
            gst.main([])

    def test_config_error_returns_1(self, monkeypatch):
        monkeypatch.setattr(
            gst, "validate_config",
            mock.Mock(side_effect=gst.ConfigError("bad config")),
        )
        rc = gst.main(["--check-only"])
        assert rc == 1





