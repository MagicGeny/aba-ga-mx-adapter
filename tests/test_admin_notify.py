"""Tenant-admin notification tests (fake GREEN-API client)."""

from __future__ import annotations

import logging

import pytest

from aba_ga_mx_adapter.admin_notify import (
    NOTIFICATION_PREFIX,
    format_notification_message,
    parse_admin_notification_task,
)
from aba_ga_mx_adapter.errors import (
    DeliveryUnknownError,
    GreenApiError,
    InvalidTaskError,
    SendMessageRejectedError,
    STATUS_SENT,
)
from aba_ga_mx_adapter.worker import AdminNotifyProcessor

from tests.adapter_test_helpers import (
    CHAT_ID,
    FakeGreenApiClient,
    TENANT_ACCOUNT_ID,
)

ADMIN_TASK = {
    "tenant_phone": "79991234567",
    "tenant_id": "99999999-8888-7777-6666-555555555555",
    "tenant_account_id": TENANT_ACCOUNT_ID,
    "chat_id": "",
    "use_chat_id": False,
    "replies": [
        {
            "user_phone": "9601701985",
            "user_name": "Ходабрыш",
            "message": "Здравствуйте, да, актуально",
            "time": "2026-11-13T16:20:00Z",
        }
    ],
}


def test_parse_valid_task():
    task = parse_admin_notification_task(ADMIN_TASK)
    assert task.tenant_phone == "79991234567"
    assert task.tenant_account_id == TENANT_ACCOUNT_ID
    assert len(task.replies) == 1
    assert task.replies[0].message == "Здравствуйте, да, актуально"


def test_parse_missing_fields_is_invalid():
    with pytest.raises(InvalidTaskError):
        parse_admin_notification_task({"tenant_phone": "x"})
    with pytest.raises(InvalidTaskError):
        parse_admin_notification_task([])
    with pytest.raises(InvalidTaskError):
        parse_admin_notification_task({"replies": []})


def test_format_notification_message():
    text = format_notification_message(parse_admin_notification_task(ADMIN_TASK))
    assert text.startswith(NOTIFICATION_PREFIX)
    assert "9601701985" in text
    assert "Ходабрыш" in text
    assert "Здравствуйте, да, актуально" in text


async def test_admin_notify_sends_by_phone_and_reports_chat():
    client = FakeGreenApiClient()
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(ADMIN_TASK)

    assert result is not None
    assert result.status == STATUS_SENT
    assert result.target_id == ""
    assert result.campaign_id == ""
    assert result.tenant_account_id == TENANT_ACCOUNT_ID
    assert result.phone_number == "9991234567"  # Go canonical form
    assert result.chat_id == CHAT_ID
    assert client.check_account_calls == ["79991234567"]
    assert len(client.send_calls) == 1
    assert client.send_calls[0][0] == CHAT_ID
    assert NOTIFICATION_PREFIX in client.send_calls[0][1]


async def test_admin_notify_uses_saved_chat_id():
    client = FakeGreenApiClient()
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    task = dict(ADMIN_TASK, use_chat_id=True, chat_id="77777777")

    result = await processor.process(task)

    assert result is not None
    assert result.chat_id == "77777777"
    assert client.check_account_calls == []
    assert client.send_calls[0][0] == "77777777"


async def test_admin_notify_account_isolation():
    client = FakeGreenApiClient()
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    task = dict(ADMIN_TASK, tenant_account_id="22222222-2222-3333-4444-555555555555")

    result = await processor.process(task)

    assert result is None
    assert client.send_calls == []
    assert client.state_calls == 0


async def test_admin_notify_invalid_phone():
    client = FakeGreenApiClient()
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(dict(ADMIN_TASK, tenant_phone="12345"))

    assert result is None
    assert client.send_calls == []


async def test_admin_notify_send_failure_returns_none():
    client = FakeGreenApiClient()
    client.send_error = GreenApiError("boom")
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(ADMIN_TASK)

    assert result is None


async def test_admin_notify_recovers_from_stale_saved_chat_id():
    """A rejected saved chat_id (old Playwright id space) is re-resolved once.

    The corrected chat_id is reported back so the orchestrator updates
    admin_chat_phone_mappings and later notifications work directly.
    """
    client = FakeGreenApiClient()
    client.send_error = SendMessageRejectedError(
        "SendMessage rejected by GREEN-API (HTTP 400, code=bad chat)"
    )
    client.send_error_once = True
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(
        dict(ADMIN_TASK, use_chat_id=True, chat_id="416466886")
    )

    assert result is not None
    assert result.status == STATUS_SENT
    assert result.chat_id == CHAT_ID
    assert [chat for chat, _ in client.send_calls] == ["416466886", CHAT_ID]
    assert client.check_account_calls == ["79991234567"]


async def test_admin_notify_logs_rejection_detail(caplog):
    client = FakeGreenApiClient()
    client.send_error = SendMessageRejectedError(
        "SendMessage rejected by GREEN-API (HTTP 400, code=bad chat)"
    )
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    with caplog.at_level(logging.INFO):
        await processor.process(dict(ADMIN_TASK, use_chat_id=True, chat_id="416466886"))

    assert "ADMIN_NOTIFY_CHAT_ID_REJECTED" in caplog.text
    assert "HTTP 400" in caplog.text


async def test_admin_notify_never_resends_after_delivery_unknown():
    client = FakeGreenApiClient()
    client.send_error = DeliveryUnknownError("timed out")
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(
        dict(ADMIN_TASK, use_chat_id=True, chat_id="416466886")
    )

    assert result is None
    assert len(client.send_calls) == 1
    assert client.check_account_calls == []


async def test_admin_notify_phone_path_rejection_is_not_retried():
    client = FakeGreenApiClient()
    client.send_error = SendMessageRejectedError("rejected")
    processor = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)

    result = await processor.process(ADMIN_TASK)

    assert result is None
    assert len(client.send_calls) == 1
    assert client.check_account_calls == ["79991234567"]
