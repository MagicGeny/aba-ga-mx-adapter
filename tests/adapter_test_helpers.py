"""Shared, side-effect-free helpers for the adapter tests.

Nothing here touches the network, RabbitMQ, or a real GREEN-API instance.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from aba_ga_mx_adapter.green_api import (
    CheckAccountResult,
    GetStateInstance,
    SendMessageResult,
)

TENANT_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
CAMPAIGN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TENANT_ID = "99999999-8888-7777-6666-555555555555"
TASK_ID = "01234567-89ab-cdef-0123-456789abcdef"
PHONE = "79991234567"
CHAT_ID = "10000000"
MESSAGE = "hello from the adapter test"


def task_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": TASK_ID,
        "campaign_id": CAMPAIGN_ID,
        "tenant_id": TENANT_ID,
        "tenant_account_id": TENANT_ACCOUNT_ID,
        "account_id": TENANT_ACCOUNT_ID,
        "messenger": "max",
        "messenger_type": "MAX",
        "phone": PHONE,
        "message_text": MESSAGE,
        "use_chat_id": False,
        "chat_id": "",
        "contact_type": "cold",
        "attachment_url": None,
        "attachment_name": None,
    }
    payload.update(overrides)
    return payload


def task_bytes(**overrides: Any) -> bytes:
    return json.dumps(task_dict(**overrides)).encode("utf-8")


def random_uuid() -> str:
    return str(uuid.uuid4())


class FakeGreenApiClient:
    """Stand-in for :class:`GreenApiMaxClient` that records calls."""

    def __init__(self) -> None:
        self.state_calls = 0
        self.check_account_calls: list[str] = []
        self.send_calls: list[tuple[str, str]] = []

        self.state = "authorized"
        self.state_error: Optional[Exception] = None
        self.rate_limited = False
        self.check_result: CheckAccountResult = CheckAccountResult(
            exist=True, chat_id=CHAT_ID, from_cache=False
        )
        self.check_error: Optional[Exception] = None
        self.send_result = SendMessageResult(id_message="1763115112345")
        self.send_error: Optional[Exception] = None
        # When True, send_error is raised once and then cleared (simulates a
        # stale chat_id being rejected, then a retry succeeding).
        self.send_error_once = False
        self.closed = False

        # Incoming notifications (ReceiveNotification / DeleteNotification).
        self.notification_batches: list[list[dict]] = []
        self.received_calls = 0
        self.deleted_receipts: list[int] = []
        self.receive_error: Optional[Exception] = None

    async def get_state_instance(self) -> GetStateInstance:
        self.state_calls += 1
        if self.state_error is not None:
            raise self.state_error
        return GetStateInstance(state=self.state)

    def check_account_rate_limited(self) -> bool:
        return self.rate_limited

    async def check_account(self, phone: str) -> CheckAccountResult:
        self.check_account_calls.append(phone)
        if self.check_error is not None:
            raise self.check_error
        return self.check_result

    async def send_message(self, chat_id: str, message: str) -> SendMessageResult:
        self.send_calls.append((chat_id, message))
        if self.send_error is not None:
            if self.send_error_once:
                error, self.send_error = self.send_error, None
                self.send_error_once = False
                raise error
            raise self.send_error
        return self.send_result

    async def aclose(self) -> None:
        self.closed = True

    async def receive_notification(self, receive_timeout: int = 10):
        self.received_calls += 1
        if self.receive_error is not None:
            raise self.receive_error
        if self.notification_batches:
            return self.notification_batches.pop(0)
        return []

    async def delete_notification(self, receipt_id: int) -> bool:
        self.deleted_receipts.append(receipt_id)
        return True


@dataclass
class FakeDelivery:
    body: bytes
    acked: bool = False
    acknowledged: list[bool] = field(default_factory=list)

    async def ack(self, multiple: bool = False) -> None:
        self.acked = True
        self.acknowledged.append(multiple)


class FakeGateway:
    """Stand-in for :class:`RabbitMqGateway` recording publish calls."""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.stopped_consuming = False
        self.handler = None
        self.notify_handler = None

        self.published_results: list[Any] = []
        self.dead_letters: list[tuple[bytes, str]] = []
        self.fail_publish_attempts = 0
        self.fail_dead_letter_attempts = 0
        self.stop_requested = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def start_consuming(self, handler) -> None:
        self.handler = handler

    async def start_consuming_notify(self, handler) -> None:
        self.notify_handler = handler

    async def stop_consuming(self) -> None:
        self.stopped_consuming = True

    def request_stop(self) -> None:
        self.stop_requested = True

    async def publish_result(self, result) -> None:
        if self.fail_publish_attempts > 0:
            self.fail_publish_attempts -= 1
            raise RuntimeError("simulated publish failure")
        self.published_results.append(result)

    async def publish_dead_letter(self, raw_body: bytes, reason: str) -> None:
        if self.fail_dead_letter_attempts > 0:
            self.fail_dead_letter_attempts -= 1
            raise RuntimeError("simulated dead-letter failure")
        self.dead_letters.append((raw_body, reason))
