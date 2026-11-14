"""Task processing tests (fake GREEN-API client; no RabbitMQ)."""

from __future__ import annotations

import logging

import pytest

from aba_ga_mx_adapter.errors import (
    AdapterError,
    CheckAccountRateLimitedError,
    DeliveryUnknownError,
    DeliveryUnknownError as _DeliveryUnknown,
    ERROR_CODE_INSTANCE_BLOCKED,
    GreenApiError,
    SessionExpiredError,
    STATUS_DELIVERY_UNKNOWN,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_USER_NOT_FOUND_BY_PHONE,
    WorkerUnavailableError,
)
from aba_ga_mx_adapter.green_api import CheckAccountResult
from aba_ga_mx_adapter.models import SendTaskPayload
from aba_ga_mx_adapter.worker import TaskProcessor

from tests.adapter_test_helpers import (
    CHAT_ID,
    FakeGreenApiClient,
    MESSAGE,
    TENANT_ACCOUNT_ID,
    TASK_ID,
    task_bytes,
)

PROCESS_ACCOUNT = TENANT_ACCOUNT_ID


def make_processor(client: FakeGreenApiClient, account: str = PROCESS_ACCOUNT):
    return TaskProcessor(tenant_account_id=account, client=client)


def parse_task(**overrides) -> SendTaskPayload:
    return SendTaskPayload.from_json(task_bytes(**overrides))


async def test_cold_contact_flow_state_check_send():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_SENT
    assert result.chat_id == CHAT_ID
    assert client.state_calls == 1
    assert client.check_account_calls == ["79991234567"]
    assert client.send_calls == [(CHAT_ID, MESSAGE)]


async def test_go_canonical_phone_is_converted_but_result_keeps_original():
    """Go sends 10 digits starting with 9; GREEN-API needs 7XXXXXXXXXX.

    The result must still carry Go's original phone_number so its chat
    mapping (phone_normalized) keeps matching.
    """
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(phone="9601701985"))

    assert result.status == STATUS_SENT
    assert client.check_account_calls == ["79601701985"]
    assert result.phone_number == "9601701985"
    assert client.send_calls == [(CHAT_ID, MESSAGE)]


async def test_warm_contact_uses_chat_id_without_check_account():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(
        parse_task(use_chat_id=True, chat_id="55555555", contact_type="warm")
    )

    assert result.status == STATUS_SENT
    assert result.chat_id == "55555555"
    assert client.check_account_calls == []
    assert client.send_calls == [("55555555", MESSAGE)]


async def test_exist_false_does_not_send():
    client = FakeGreenApiClient()
    client.check_result = CheckAccountResult(exist=False, chat_id="", from_cache=None)
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_USER_NOT_FOUND_BY_PHONE
    assert result.error_code == "USER_NOT_FOUND_BY_PHONE"
    assert client.send_calls == []


async def test_state_not_authorized_does_not_send():
    client = FakeGreenApiClient()
    client.state = "notAuthorized"
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == "SESSION_EXPIRED"
    assert client.send_calls == []


async def test_state_blocked_does_not_send():
    client = FakeGreenApiClient()
    client.state = "blocked"
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == ERROR_CODE_INSTANCE_BLOCKED
    assert client.send_calls == []


async def test_state_starting_is_worker_unavailable():
    client = FakeGreenApiClient()
    client.state = "starting"
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.error_code == "WORKER_UNAVAILABLE"
    assert client.send_calls == []


async def test_tenant_account_mismatch_never_calls_api():
    client = FakeGreenApiClient()
    processor = make_processor(client, account="22222222-2222-3333-4444-555555555555")

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == "ACCOUNT_MISMATCH"
    assert client.state_calls == 0
    assert client.check_account_calls == []
    assert client.send_calls == []
    # The original task identifiers are used in the result.
    assert result.target_id == TASK_ID
    assert result.tenant_account_id == TENANT_ACCOUNT_ID


async def test_account_id_mismatch_never_calls_api():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(
        parse_task(account_id="33333333-3333-3333-3333-333333333333")
    )

    assert result.error_code == "ACCOUNT_MISMATCH"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_attachment_is_unsupported_without_sending():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(
        parse_task(attachment_url="https://example.com/file.pdf")
    )

    assert result.status == STATUS_FAILED
    assert result.error_code == "UNSUPPORTED_ATTACHMENT"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_invalid_phone_does_not_send():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(phone="12345"))

    assert result.status == STATUS_FAILED
    assert result.error_code == "INVALID_PHONE"
    assert client.state_calls == 1  # state gate runs before phone resolution
    assert client.check_account_calls == []
    assert client.send_calls == []


async def test_empty_message_is_rejected_without_api_call():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(message_text=""))

    assert result.error_code == "INVALID_MESSAGE_TEXT"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_message_over_limit_is_rejected_without_api_call():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(message_text="x" * 4001))

    assert result.error_code == "INVALID_MESSAGE_TEXT"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_use_chat_id_without_chat_id_is_rejected():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(
        parse_task(use_chat_id=True, chat_id="")
    )

    assert result.error_code == "MISSING_CHAT_ID"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_wrong_messenger_type_is_rejected():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(messenger_type="VK"))

    assert result.error_code == "UNSUPPORTED_MESSENGER_TYPE"
    assert client.state_calls == 0
    assert client.send_calls == []


async def test_messenger_type_is_case_insensitive():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task(messenger_type="max"))

    assert result.status == STATUS_SENT


async def test_check_account_cooldown_blocks_cold_tasks():
    client = FakeGreenApiClient()
    client.rate_limited = True
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == "CHECK_ACCOUNT_RATE_LIMITED"
    assert client.check_account_calls == []
    assert client.send_calls == []


async def test_send_timeout_is_delivery_unknown_with_chat_id():
    client = FakeGreenApiClient()
    client.send_error = DeliveryUnknownError("timed out")
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_DELIVERY_UNKNOWN
    assert result.error_code == "DELIVERY_UNKNOWN"
    assert result.chat_id == CHAT_ID


async def test_send_connect_error_is_worker_unavailable_without_chat_id():
    client = FakeGreenApiClient()
    client.send_error = WorkerUnavailableError("no route")
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == "WORKER_UNAVAILABLE"


async def test_unknown_exception_before_send_is_internal_error():
    client = FakeGreenApiClient()
    client.state_error = RuntimeError("boom")
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_FAILED
    assert result.error_code == "INTERNAL_ERROR"


async def test_unknown_exception_from_send_is_delivery_unknown():
    client = FakeGreenApiClient()
    client.send_error = RuntimeError("boom")
    processor = make_processor(client)

    result = await processor.process_task(parse_task())

    assert result.status == STATUS_DELIVERY_UNKNOWN
    assert result.error_code == "DELIVERY_UNKNOWN"


async def test_success_result_has_all_uuids():
    client = FakeGreenApiClient()
    processor = make_processor(client)

    result = await processor.process_task(parse_task())
    data = result.to_dict()

    assert data["target_id"] == TASK_ID
    assert data["tenant_account_id"] == TENANT_ACCOUNT_ID
    assert data["account_id"] == TENANT_ACCOUNT_ID
    assert data["phone_number"] == "79991234567"
    assert data["messenger_type"] == "MAX"


async def test_message_text_is_not_logged(caplog):
    client = FakeGreenApiClient()
    processor = make_processor(client)
    secret_message = "EXTREMELY-SECRET-USER-TEXT"

    with caplog.at_level(logging.DEBUG):
        await processor.process_task(parse_task(message_text=secret_message))

    assert secret_message not in caplog.text
