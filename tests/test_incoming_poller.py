"""Incoming-reply poller and admin-notify consumer tests."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from aba_ga_mx_adapter.errors import (
    STATUS_DELIVERED,
    STATUS_REPLIED,
    STATUS_SENT,
    WorkerUnavailableError,
)
from aba_ga_mx_adapter.worker import (
    AdapterWorker,
    AdminNotifyProcessor,
    IncomingMessagePoller,
    TaskProcessor,
)

from tests.adapter_test_helpers import (
    CHAT_ID,
    FakeDelivery,
    FakeGateway,
    FakeGreenApiClient,
    TENANT_ACCOUNT_ID,
    task_bytes,
)

TENANT_ID = "99999999-8888-7777-6666-555555555555"

ADMIN_TASK = {
    "tenant_phone": "79991234567",
    "tenant_id": TENANT_ID,
    "tenant_account_id": TENANT_ACCOUNT_ID,
    "chat_id": "",
    "use_chat_id": False,
    "replies": [
        {"user_phone": "9601701985", "user_name": "X", "message": "да", "time": "2026-11-13T16:20:00Z"}
    ],
}


def _reply_notification(receipt_id=10, chat_id="155395237", text="ответ"):
    return {
        "receiptId": receipt_id,
        "body": {
            "typeWebhook": "incomingMessageReceived",
            "timestamp": 1763115112,
            "senderData": {"chatId": chat_id, "chatType": "user"},
            "messageData": {
                "typeMessage": "textMessage",
                "textMessageData": {"textMessage": text},
            },
        },
    }


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


def make_poller(client, gateway) -> IncomingMessagePoller:
    return IncomingMessagePoller(
        tenant_account_id=TENANT_ACCOUNT_ID,
        client=client,
        gateway=gateway,
        receive_timeout=5,
    )


async def test_poll_once_publishes_reply_and_deletes_notification():
    client = FakeGreenApiClient()
    gateway = FakeGateway()
    client.notification_batches = [[_reply_notification()]]
    poller = make_poller(client, gateway)

    count = await poller.poll_once()

    assert count == 1
    assert len(gateway.published_results) == 1
    assert gateway.published_results[0].status == STATUS_REPLIED
    assert gateway.published_results[0].reply_text == "ответ"
    assert client.deleted_receipts == [10]


async def test_poll_once_deletes_irrelevant_notification_without_publishing(caplog):
    client = FakeGreenApiClient()
    gateway = FakeGateway()
    client.notification_batches = [[{"receiptId": 5, "body": {"typeWebhook": "stateInstanceChanged"}}]]
    poller = make_poller(client, gateway)

    with caplog.at_level(logging.INFO):
        await poller.poll_once()

    assert gateway.published_results == []
    assert client.deleted_receipts == [5]
    # The ignored notification is now visible in the logs.
    assert "INCOMING_NOTIFICATION_IGNORED" in caplog.text
    assert "stateInstanceChanged" in caplog.text


async def test_poll_once_publishes_status_results():
    client = FakeGreenApiClient()
    gateway = FakeGateway()
    client.notification_batches = [[{
        "receiptId": 7,
        "body": {"typeWebhook": "outgoingMessageStatus", "chatId": CHAT_ID, "status": "delivered", "timestamp": 1},
    }]]
    poller = make_poller(client, gateway)

    await poller.poll_once()

    assert [r.status for r in gateway.published_results] == [STATUS_DELIVERED]


async def test_run_forever_starts_poller_and_stops_on_request():
    client = FakeGreenApiClient()
    gateway = FakeGateway()
    processor = TaskProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    notify = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    poller = make_poller(client, gateway)
    worker = AdapterWorker(
        gateway=gateway, processor=processor, admin_notify=notify, incoming_poller=poller
    )

    run = asyncio.create_task(worker.run_forever())
    for _ in range(20):
        if gateway.notify_handler is not None:
            break
        await asyncio.sleep(0)

    assert gateway.notify_handler is not None
    worker.request_stop()
    await asyncio.wait_for(run, timeout=2)
    assert gateway.closed is True


# ---------------------------------------------------------------------------
# Admin notify delivery
# ---------------------------------------------------------------------------


def make_worker_with_notify(gateway, client=None):
    client = client or FakeGreenApiClient()
    processor = TaskProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    notify = AdminNotifyProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    worker = AdapterWorker(gateway=gateway, processor=processor, admin_notify=notify)
    return worker, client


async def test_notify_delivery_publishes_sent_result_and_acks():
    gateway = FakeGateway()
    worker, client = make_worker_with_notify(gateway)
    delivery = FakeDelivery(body=json.dumps(ADMIN_TASK).encode())

    await worker.handle_notify_delivery(delivery)

    assert delivery.acked is True
    assert len(gateway.published_results) == 1
    result = gateway.published_results[0]
    assert result.status == STATUS_SENT
    assert result.target_id == ""
    assert result.campaign_id == ""
    assert result.tenant_account_id == TENANT_ACCOUNT_ID
    assert result.chat_id == CHAT_ID


async def test_notify_delivery_invalid_json_is_acked():
    gateway = FakeGateway()
    worker, _ = make_worker_with_notify(gateway)
    delivery = FakeDelivery(body=b"{not json")

    await worker.handle_notify_delivery(delivery)

    assert delivery.acked is True
    assert gateway.published_results == []


async def test_notify_delivery_account_mismatch_is_acked_without_send():
    gateway = FakeGateway()
    worker, client = make_worker_with_notify(gateway)
    task = dict(ADMIN_TASK, tenant_account_id="22222222-2222-3333-4444-555555555555")
    delivery = FakeDelivery(body=json.dumps(task).encode())

    await worker.handle_notify_delivery(delivery)

    assert delivery.acked is True
    assert gateway.published_results == []
    assert client.send_calls == []


async def test_notify_delivery_without_processor_is_acked():
    gateway = FakeGateway()
    client = FakeGreenApiClient()
    processor = TaskProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    worker = AdapterWorker(gateway=gateway, processor=processor)  # no admin_notify
    delivery = FakeDelivery(body=json.dumps(ADMIN_TASK).encode())

    await worker.handle_notify_delivery(delivery)

    assert delivery.acked is True
