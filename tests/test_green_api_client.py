"""GREEN-API client tests (all HTTP mocked with respx; no network)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from aba_ga_mx_adapter.errors import (
    CheckAccountRateLimitedError,
    DeliveryUnknownError,
    ERROR_CODE_INSTANCE_BLOCKED,
    ERROR_CODE_INSTANCE_SUSPENDED,
    ERROR_CODE_UNKNOWN_INSTANCE_STATE,
    GreenApiError,
    MessageTooLongError,
    SessionExpiredError,
    SendMessageRejectedError,
    STATUS_DELIVERY_UNKNOWN,
    WorkerUnavailableError,
)
from aba_ga_mx_adapter.green_api import (
    CHECK_ACCOUNT_COOLDOWN_SECONDS,
    GreenApiMaxClient,
    to_green_api_phone,
)

BASE = "https://example.green-api.com"
ID = "1101000001"
TOKEN = "test-token-value"

STATE_URL = f"{BASE}/waInstance{ID}/getStateInstance/{TOKEN}"
CHECK_URL = f"{BASE}/waInstance{ID}/checkAccount/{TOKEN}"
SEND_URL = f"{BASE}/waInstance{ID}/sendMessage/{TOKEN}"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(clock=None) -> GreenApiMaxClient:
    return GreenApiMaxClient(
        base_url=BASE,
        id_instance=ID,
        token_instance=TOKEN,
        timeout_seconds=5.0,
        clock=clock or FakeClock(),
    )


# ---------------------------------------------------------------------------
# GetStateInstance
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_state_instance_authorized():
    route = respx.get(STATE_URL).respond(200, json={"stateInstance": "authorized"})
    async with make_client() as client:
        state = await client.get_state_instance()
    assert state.state == "authorized"
    assert state.is_authorized is True
    assert route.called
    assert route.calls.last.request.method == "GET"


@pytest.mark.parametrize("state", ["notAuthorized", "pendingPassword"])
@respx.mock
async def test_get_state_instance_auth_problems_raise_session_expired(state):
    respx.get(STATE_URL).respond(200, json={"stateInstance": state})
    async with make_client() as client:
        with pytest.raises(SessionExpiredError):
            await client.get_state_instance()


@respx.mock
async def test_get_state_instance_starting_is_worker_unavailable():
    respx.get(STATE_URL).respond(200, json={"stateInstance": "starting"})
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.get_state_instance()


@respx.mock
async def test_get_state_instance_blocked():
    respx.get(STATE_URL).respond(200, json={"stateInstance": "blocked"})
    async with make_client() as client:
        with pytest.raises(GreenApiError) as exc:
            await client.get_state_instance()
    assert exc.value.error_code == ERROR_CODE_INSTANCE_BLOCKED


@respx.mock
async def test_get_state_instance_suspended():
    respx.get(STATE_URL).respond(200, json={"stateInstance": "suspended"})
    async with make_client() as client:
        with pytest.raises(GreenApiError) as exc:
            await client.get_state_instance()
    assert exc.value.error_code == ERROR_CODE_INSTANCE_SUSPENDED


@respx.mock
async def test_get_state_instance_unknown_state():
    respx.get(STATE_URL).respond(200, json={"stateInstance": "wat"})
    async with make_client() as client:
        with pytest.raises(GreenApiError) as exc:
            await client.get_state_instance()
    assert exc.value.error_code == ERROR_CODE_UNKNOWN_INSTANCE_STATE


@respx.mock
async def test_get_state_instance_http_error_status():
    respx.get(STATE_URL).respond(500, json={"message": "boom"})
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.get_state_instance()


@respx.mock
async def test_get_state_instance_unauthorized_status():
    respx.get(STATE_URL).respond(401, json={"message": "unauthorized"})
    async with make_client() as client:
        with pytest.raises(SessionExpiredError):
            await client.get_state_instance()


@respx.mock
async def test_get_state_instance_transport_error():
    respx.get(STATE_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.get_state_instance()


# ---------------------------------------------------------------------------
# CheckAccount
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_account_sends_integer_phone_and_no_force():
    route = respx.post(CHECK_URL).respond(
        200, json={"exist": True, "chatId": "10000000", "fromCache": False}
    )
    async with make_client() as client:
        result = await client.check_account("79991234567")

    assert result.exist is True
    assert result.chat_id == "10000000"
    assert result.from_cache is False

    body = route.calls.last.request.content
    payload = json.loads(body)
    assert payload == {"phoneNumber": 79991234567}
    assert "force" not in payload
    # The number must be a JSON integer, not a quoted string.
    assert b'"phoneNumber": 79991234567' in body


@respx.mock
async def test_check_account_exist_false():
    respx.post(CHECK_URL).respond(200, json={"exist": False})
    async with make_client() as client:
        result = await client.check_account("79991234567")
    assert result.exist is False
    assert result.chat_id == ""


@respx.mock
async def test_check_account_status_false_is_api_error_not_user_not_found():
    respx.post(CHECK_URL).respond(200, json={"status": False, "exist": False})
    async with make_client() as client:
        with pytest.raises(GreenApiError) as exc:
            await client.check_account("79991234567")
    # It must NOT be reported as exist=false / user-not-found.
    assert exc.value.error_code == "GREEN_API_ERROR"


@respx.mock
async def test_check_account_missing_exist_is_api_error():
    respx.post(CHECK_URL).respond(200, json={"chatId": "10000000"})
    async with make_client() as client:
        with pytest.raises(GreenApiError):
            await client.check_account("79991234567")


@respx.mock
async def test_check_account_exist_true_without_chat_id_is_api_error():
    respx.post(CHECK_URL).respond(200, json={"exist": True})
    async with make_client() as client:
        with pytest.raises(GreenApiError):
            await client.check_account("79991234567")


@respx.mock
async def test_check_account_http_469_status_sets_cooldown():
    clock = FakeClock()
    respx.post(CHECK_URL).respond(469, json={"code": 469, "message": "limit"})
    client = make_client(clock)
    async with client:
        with pytest.raises(CheckAccountRateLimitedError):
            await client.check_account("79991234567")
        assert client.check_account_rate_limited() is True

        clock.advance(CHECK_ACCOUNT_COOLDOWN_SECONDS - 1)
        assert client.check_account_rate_limited() is True

        clock.advance(2)
        assert client.check_account_rate_limited() is False


@respx.mock
async def test_check_account_body_code_469_sets_cooldown():
    respx.post(CHECK_URL).respond(200, json={"code": 469})
    async with make_client() as client:
        with pytest.raises(CheckAccountRateLimitedError):
            await client.check_account("79991234567")
        assert client.check_account_rate_limited() is True


@respx.mock
async def test_check_account_refused_while_in_cooldown_without_http_call():
    route = respx.post(CHECK_URL).respond(469, json={"code": 469})
    clock = FakeClock()
    client = make_client(clock)
    async with client:
        with pytest.raises(CheckAccountRateLimitedError):
            await client.check_account("79991234567")
        calls_before = len(route.calls)
        with pytest.raises(CheckAccountRateLimitedError):
            await client.check_account("79991234567")
        assert len(route.calls) == calls_before


@respx.mock
async def test_check_account_transport_error_is_worker_unavailable():
    respx.post(CHECK_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.check_account("79991234567")


# ---------------------------------------------------------------------------
# SendMessage
# ---------------------------------------------------------------------------


@respx.mock
async def test_send_message_body_and_success():
    route = respx.post(SEND_URL).respond(200, json={"idMessage": "1763115112345"})
    async with make_client() as client:
        result = await client.send_message("10000000", "hello")

    assert result.id_message == "1763115112345"
    payload = json.loads(route.calls.last.request.content)
    assert payload == {"chatId": "10000000", "message": "hello"}


@respx.mock
async def test_send_message_4000_chars_is_allowed():
    route = respx.post(SEND_URL).respond(200, json={"idMessage": "1"})
    message = "x" * 4000
    async with make_client() as client:
        result = await client.send_message("10000000", message)
    assert result.id_message == "1"
    assert route.called


@respx.mock
async def test_send_message_over_4000_chars_is_rejected_without_request():
    route = respx.post(SEND_URL).respond(200, json={"idMessage": "1"})
    async with make_client() as client:
        with pytest.raises(MessageTooLongError):
            await client.send_message("10000000", "x" * 4001)
    assert not route.called


@respx.mock
async def test_send_message_read_timeout_is_delivery_unknown():
    respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    async with make_client() as client:
        with pytest.raises(DeliveryUnknownError) as exc:
            await client.send_message("10000000", "hello")
    assert exc.value.task_status == STATUS_DELIVERY_UNKNOWN
    assert exc.value.error_code == "DELIVERY_UNKNOWN"


@respx.mock
async def test_send_message_connect_error_is_worker_unavailable():
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.send_message("10000000", "hello")


@respx.mock
async def test_send_message_api_error_response_is_not_success():
    respx.post(SEND_URL).respond(400, json={"message": "bad request"})
    async with make_client() as client:
        with pytest.raises(SendMessageRejectedError):
            await client.send_message("10000000", "hello")


@respx.mock
async def test_send_message_missing_id_message_is_delivery_unknown():
    respx.post(SEND_URL).respond(200, json={"status": "ok"})
    async with make_client() as client:
        with pytest.raises(DeliveryUnknownError):
            await client.send_message("10000000", "hello")


# ---------------------------------------------------------------------------
# ReceiveNotification / DeleteNotification
# ---------------------------------------------------------------------------

RECEIVE_URL = f"{BASE}/waInstance{ID}/receiveNotification/{TOKEN}"
DELETE_URL = f"{BASE}/waInstance{ID}/deleteNotification/{TOKEN}/10"


@respx.mock
async def test_receive_notification_returns_list():
    payload = [{"receiptId": 10, "body": {"typeWebhook": "x"}}]
    route = respx.get(RECEIVE_URL).respond(200, json=payload)
    async with make_client() as client:
        notifications = await client.receive_notification(receive_timeout=10)
    assert notifications == payload
    assert route.calls.last.request.url.params["receiveTimeout"] == "10"


@respx.mock
async def test_receive_notification_empty_returns_empty_list():
    respx.get(RECEIVE_URL).respond(200, text="")
    async with make_client() as client:
        assert await client.receive_notification() == []


@respx.mock
async def test_receive_notification_timeout_returns_empty_list():
    respx.get(RECEIVE_URL).mock(side_effect=httpx.ReadTimeout("no data"))
    async with make_client() as client:
        assert await client.receive_notification(receive_timeout=10) == []


@respx.mock
async def test_receive_notification_http_error_is_worker_unavailable():
    respx.get(RECEIVE_URL).respond(500, json={"message": "boom"})
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.receive_notification()


@respx.mock
async def test_delete_notification_uses_receipt_id_in_path():
    route = respx.delete(DELETE_URL).respond(200, json={"result": True})
    async with make_client() as client:
        assert await client.delete_notification(10) is True
    assert route.called


@respx.mock
async def test_delete_notification_transport_error():
    respx.delete(DELETE_URL).mock(side_effect=httpx.ConnectError("no route"))
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError):
            await client.delete_notification(10)


# ---------------------------------------------------------------------------
# GetSettings / SetSettings
# ---------------------------------------------------------------------------

SETTINGS_URL = f"{BASE}/waInstance{ID}/getSettings/{TOKEN}"
SET_SETTINGS_URL = f"{BASE}/waInstance{ID}/setSettings/{TOKEN}"


@respx.mock
async def test_get_settings_returns_object():
    respx.get(SETTINGS_URL).respond(
        200, json={"webhookUrl": "", "incomingWebhook": "no"}
    )
    async with make_client() as client:
        settings = await client.get_settings()
    assert settings == {"webhookUrl": "", "incomingWebhook": "no"}


@respx.mock
async def test_get_settings_error_is_classified():
    respx.get(SETTINGS_URL).respond(401, json={"message": "unauthorized"})
    async with make_client() as client:
        with pytest.raises(SessionExpiredError):
            await client.get_settings()


@respx.mock
async def test_set_settings_posts_partial_body():
    route = respx.post(SET_SETTINGS_URL).respond(200, json={"saveSettings": True})
    async with make_client() as client:
        assert await client.set_settings({"incomingWebhook": "yes"}) is True
    assert json.loads(route.calls.last.request.content) == {"incomingWebhook": "yes"}


@respx.mock
async def test_set_settings_rejected_response():
    respx.post(SET_SETTINGS_URL).respond(400, json={"message": "bad request"})
    async with make_client() as client:
        with pytest.raises(GreenApiError):
            await client.set_settings({"incomingWebhook": "yes"})


# ---------------------------------------------------------------------------
# Phone format conversion (orchestrator -> GREEN-API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Go orchestrator canonical form: 10 digits starting with 9.
        ("9601701985", "79601701985"),
        ("9991234567", "79991234567"),
        # Already-international numbers are passed through unchanged.
        ("79991234567", "79991234567"),
        ("375291234567", "375291234567"),
        (" 9601701985 ", "79601701985"),
        # Raw 8-prefixed (Go NormalizePhone) forms are handled too.
        ("89601701985", "79601701985"),
        ("+7 960 170-19-85", "79601701985"),
    ],
)
def test_to_green_api_phone_converts(raw, expected):
    assert to_green_api_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "12345", "7960170198", "796017019855", "8601701985", "abcdefghij", "49601701985"],
)
def test_to_green_api_phone_rejects_invalid(raw):
    assert to_green_api_phone(raw) is None


@respx.mock
async def test_check_account_accepts_converted_go_canonical_phone():
    """The value actually sent to GREEN-API is the 11-digit international form."""
    route = respx.post(CHECK_URL).respond(
        200, json={"exist": True, "chatId": "10000000", "fromCache": False}
    )
    async with make_client() as client:
        await client.check_account(to_green_api_phone("9601701985"))

    payload = json.loads(route.calls.last.request.content)
    assert payload == {"phoneNumber": 79601701985}


# ---------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------


@respx.mock
async def test_token_never_appears_in_exceptions():
    respx.get(STATE_URL).mock(side_effect=httpx.ConnectError("refused"))
    async with make_client() as client:
        with pytest.raises(WorkerUnavailableError) as exc:
            await client.get_state_instance()
    assert TOKEN not in str(exc.value)


@respx.mock
async def test_token_never_appears_in_logs(caplog):
    respx.post(CHECK_URL).respond(469, json={"code": 469})
    with caplog.at_level(logging.DEBUG):
        async with make_client() as client:
            with pytest.raises(CheckAccountRateLimitedError):
                await client.check_account("79991234567")
    assert TOKEN not in caplog.text
    assert "waInstance" not in caplog.text
