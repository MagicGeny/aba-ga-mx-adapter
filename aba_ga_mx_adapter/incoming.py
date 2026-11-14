"""Incoming GREEN-API MAX notifications -> orchestrator results.

Converts a GREEN-API notification (as returned by ``ReceiveNotification``)
into :class:`~aba_ga_mx_adapter.models.TargetResult` items the Go orchestrator
understands:

* ``incomingMessageReceived`` (text, personal chat) -> ``replied`` result with
  ``reply_text``; the orchestrator resolves the target by ``chat_id``;
* ``outgoingMessageStatus`` ``delivered`` / ``read`` -> ``delivered`` /
  ``viewed`` results.

Group/channel/bot messages and non-text messages are ignored, matching the
Playwright adapter (which only forwarded personal text replies).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .admin_notify import NOTIFICATION_PREFIX
from .errors import STATUS_DELIVERED, STATUS_VIEWED
from .models import TargetResult, utc_now_iso

logger = logging.getLogger(__name__)

INCOMING_MESSAGE_RECEIVED = "incomingMessageReceived"
OUTGOING_MESSAGE_STATUS = "outgoingMessageStatus"

TEXT_MESSAGE = "textMessage"

GREEN_API_STATUS_DELIVERED = "delivered"
GREEN_API_STATUS_READ = "read"

_STATUS_TO_RESULT = {
    GREEN_API_STATUS_DELIVERED: STATUS_DELIVERED,
    GREEN_API_STATUS_READ: STATUS_VIEWED,
}

_PERSONAL_CHAT_TYPES = {"", "user"}


def _to_iso(timestamp: Any) -> str:
    if isinstance(timestamp, bool):  # bool is an int subclass
        return utc_now_iso()
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    if isinstance(timestamp, str) and timestamp.strip():
        return timestamp.strip()
    return utc_now_iso()


def notification_to_results(
    notification: Any, *, tenant_account_id: str
) -> list[TargetResult]:
    """Map one ``ReceiveNotification`` item to zero or more results."""
    if not isinstance(notification, dict):
        return []
    body = notification.get("body")
    if not isinstance(body, dict):
        return []

    type_webhook = body.get("typeWebhook")
    if type_webhook == INCOMING_MESSAGE_RECEIVED:
        return _incoming_message_results(body, tenant_account_id)
    if type_webhook == OUTGOING_MESSAGE_STATUS:
        return _status_results(body, tenant_account_id)
    return []


def notification_summary(notification: Any) -> str:
    """Secret-free structural summary of a notification (never the text)."""
    if not isinstance(notification, dict):
        return "type=unknown"
    body = notification.get("body")
    if not isinstance(body, dict):
        return "type=unknown"
    parts = [f"type={body.get('typeWebhook')}"]
    if body.get("typeWebhook") == INCOMING_MESSAGE_RECEIVED:
        sender = body.get("senderData")
        message_data = body.get("messageData")
        if isinstance(sender, dict):
            parts.append(f"chatType={sender.get('chatType')}")
            parts.append("chatId=ok" if sender.get("chatId") else "chatId=missing")
        if isinstance(message_data, dict):
            parts.append(f"typeMessage={message_data.get('typeMessage')}")
    elif body.get("typeWebhook") == OUTGOING_MESSAGE_STATUS:
        parts.append(f"status={body.get('status')}")
        parts.append("chatId=ok" if body.get("chatId") else "chatId=missing")
    return " ".join(str(part) for part in parts)


def _incoming_message_results(
    body: dict, tenant_account_id: str
) -> list[TargetResult]:
    sender = body.get("senderData")
    if not isinstance(sender, dict):
        return []

    chat_type = str(sender.get("chatType") or "").strip().lower()
    if chat_type not in _PERSONAL_CHAT_TYPES:
        return []

    chat_id = str(sender.get("chatId") or "").strip()
    if not chat_id:
        return []

    message_data = body.get("messageData")
    if not isinstance(message_data, dict):
        return []
    if message_data.get("typeMessage") != TEXT_MESSAGE:
        return []

    text_data = message_data.get("textMessageData")
    text = ""
    if isinstance(text_data, dict):
        text = str(text_data.get("textMessage") or "").strip()
    if not text:
        return []
    # Never treat our own admin notification echo as a customer reply.
    if text.startswith(NOTIFICATION_PREFIX):
        return []

    return [
        TargetResult.for_incoming_reply(
            tenant_account_id=tenant_account_id,
            chat_id=chat_id,
            reply_text=text,
            timestamp=_to_iso(body.get("timestamp")),
        )
    ]


def _status_results(body: dict, tenant_account_id: str) -> list[TargetResult]:
    chat_id = str(body.get("chatId") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    mapped = _STATUS_TO_RESULT.get(status)
    if not chat_id or mapped is None:
        return []
    return [
        TargetResult.for_incoming_status(
            tenant_account_id=tenant_account_id,
            chat_id=chat_id,
            status=mapped,
            timestamp=_to_iso(body.get("timestamp")),
        )
    ]
