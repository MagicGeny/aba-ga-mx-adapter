"""Incoming task payload and outgoing result models.

The wire format mirrors the Go orchestrator exactly:

* ``domain.SendTaskPayload`` (``internal/domain/models.go``) is what the adapter
  consumes;
* ``domain.TargetResult`` is what the adapter publishes.

The result JSON deliberately contains **only** the fields the current Go
``TargetResult`` declares. Unknown fields are dropped by Go's custom
``UnmarshalJSON``, so adding new ones (e.g. ``provider_message_id``) requires a
coordinated Go change and is out of scope here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .errors import (
    InvalidTaskError,
    MESSENGER_TYPE_MAX,
    STATUS_REPLIED,
    STATUS_SENT,
)

MAX_MESSAGE_LENGTH = 4000

_UUID_FIELDS = ("task_id", "campaign_id", "tenant_id", "tenant_account_id")


def utc_now_iso() -> str:
    """Current UTC timestamp as ISO-8601 with a ``Z`` suffix (RFC3339)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidTaskError(f"{field_name} is missing or not a string")
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        raise InvalidTaskError(f"{field_name} is not a valid UUID") from None


def _optional_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return ""


@dataclass(frozen=True)
class SendTaskPayload:
    """One outgoing send task as published by the Go orchestrator."""

    task_id: str
    campaign_id: str
    tenant_id: str
    tenant_account_id: str
    account_id: str
    messenger: str
    messenger_type: str
    phone: str
    message_text: str
    use_chat_id: bool
    chat_id: str
    contact_type: str
    attachment_url: Optional[str]
    attachment_name: Optional[str]

    @property
    def target_id(self) -> str:
        """The worker ``task_id`` IS ``campaign_targets.id`` (== ``target_id``)."""
        return self.task_id

    def has_attachment(self) -> bool:
        return bool((self.attachment_url or "").strip()) or bool(
            (self.attachment_name or "").strip()
        )

    @classmethod
    def from_json(cls, data: Any) -> "SendTaskPayload":
        """Parse and structurally validate a task.

        Raises :class:`InvalidTaskError` when the message is structurally
        broken and cannot be turned into a result (the caller dead-letters it).
        Field-level validation that still allows a result is done later, in the
        task processor.
        """
        if isinstance(data, (bytes, bytearray, memoryview)):
            data = bytes(data)
        if isinstance(data, (bytes, str)):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                raise InvalidTaskError("invalid_json") from None

        if not isinstance(data, dict):
            raise InvalidTaskError("payload_is_not_a_json_object")

        uuids: dict[str, str] = {}
        for name in _UUID_FIELDS:
            uuids[name] = _canonical_uuid(data.get(name), name)

        use_chat_id = data.get("use_chat_id", False)
        if not isinstance(use_chat_id, bool):
            raise InvalidTaskError("use_chat_id is not a boolean")

        account_id = data.get("account_id")
        account_id_str = account_id.strip() if isinstance(account_id, str) else ""

        attachment_url = data.get("attachment_url")
        attachment_name = data.get("attachment_name")

        return cls(
            task_id=uuids["task_id"],
            campaign_id=uuids["campaign_id"],
            tenant_id=uuids["tenant_id"],
            tenant_account_id=uuids["tenant_account_id"],
            account_id=account_id_str,
            messenger=_optional_str(data.get("messenger")),
            messenger_type=_optional_str(data.get("messenger_type")),
            phone=_optional_str(data.get("phone")),
            message_text=_optional_str(data.get("message_text")),
            use_chat_id=use_chat_id,
            chat_id=_optional_str(data.get("chat_id")),
            contact_type=_optional_str(data.get("contact_type")),
            attachment_url=_raw_optional_str(attachment_url),
            attachment_name=_raw_optional_str(attachment_name),
        )


def _raw_optional_str(value: Any) -> Optional[str]:
    """Return ``None`` for absent/empty attachments, the value otherwise.

    Non-string attachment values are coerced to a string so a malformed
    attachment is still reported as ``UNSUPPORTED_ATTACHMENT`` rather than
    silently ignored.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    return str(value)


# Exact key set accepted by the current Go ``TargetResult`` (plus the optional
# ``reply_text``, which the send adapter never emits). ``provider_message_id``
# is intentionally absent.
RESULT_FIELDS = (
    "target_id",
    "campaign_id",
    "tenant_id",
    "tenant_account_id",
    "account_id",
    "phone_number",
    "status",
    "error_code",
    "error_message",
    "timestamp",
    "chat_id",
    "messenger_type",
)


@dataclass
class TargetResult:
    """A result published to ``tasks.messages.results_replies_queue``."""

    target_id: str
    campaign_id: str
    tenant_id: str
    tenant_account_id: str
    account_id: str
    phone_number: str
    status: str
    error_code: str = ""
    error_message: Optional[str] = None
    timestamp: str = field(default_factory=utc_now_iso)
    chat_id: str = ""
    messenger_type: str = MESSENGER_TYPE_MAX
    reply_text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target_id": self.target_id,
            "campaign_id": self.campaign_id,
            "tenant_id": self.tenant_id,
            "tenant_account_id": self.tenant_account_id,
            "account_id": self.account_id,
            "phone_number": self.phone_number,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "timestamp": self.timestamp,
            "chat_id": self.chat_id,
            "messenger_type": self.messenger_type,
        }
        # Mirrors the Go ``reply_text,omitempty`` pointer: present only for
        # inbound replies.
        if self.reply_text is not None:
            data["reply_text"] = self.reply_text
        return data

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")

    @classmethod
    def for_task(
        cls,
        task: SendTaskPayload,
        *,
        status: str,
        error_code: str = "",
        error_message: Optional[str] = None,
        chat_id: str = "",
        timestamp: Optional[str] = None,
    ) -> "TargetResult":
        """Build a result carrying every identifier the Go orchestrator expects."""
        return cls(
            target_id=task.task_id,
            campaign_id=task.campaign_id,
            tenant_id=task.tenant_id,
            tenant_account_id=task.tenant_account_id,
            account_id=task.account_id,
            phone_number=task.phone,
            status=status,
            error_code=error_code,
            error_message=error_message,
            timestamp=timestamp or utc_now_iso(),
            chat_id=chat_id,
            messenger_type=MESSENGER_TYPE_MAX,
        )

    @classmethod
    def sent(cls, task: SendTaskPayload, *, chat_id: str) -> "TargetResult":
        return cls.for_task(task, status=STATUS_SENT, chat_id=chat_id)

    @classmethod
    def for_incoming_reply(
        cls,
        *,
        tenant_account_id: str,
        chat_id: str,
        reply_text: str,
        timestamp: Optional[str] = None,
    ) -> "TargetResult":
        """An inbound user reply.

        ``target_id``/``campaign_id``/``phone_number`` are intentionally empty:
        the Go ``ResultConsumer`` resolves them from ``chat_id`` via
        ``chat_phone_mappings`` and then records the reply.
        """
        return cls(
            target_id="",
            campaign_id="",
            tenant_id="",
            tenant_account_id=tenant_account_id,
            account_id=tenant_account_id,
            phone_number="",
            status=STATUS_REPLIED,
            timestamp=timestamp or utc_now_iso(),
            chat_id=chat_id,
            messenger_type=MESSENGER_TYPE_MAX,
            reply_text=reply_text,
        )

    @classmethod
    def for_incoming_status(
        cls,
        *,
        tenant_account_id: str,
        chat_id: str,
        status: str,
        timestamp: Optional[str] = None,
    ) -> "TargetResult":
        """A delivery/read status for a message we sent (delivered/viewed)."""
        return cls(
            target_id="",
            campaign_id="",
            tenant_id="",
            tenant_account_id=tenant_account_id,
            account_id=tenant_account_id,
            phone_number="",
            status=status,
            timestamp=timestamp or utc_now_iso(),
            chat_id=chat_id,
            messenger_type=MESSENGER_TYPE_MAX,
        )

    @classmethod
    def for_admin_notify_sent(
        cls,
        *,
        tenant_id: str,
        tenant_account_id: str,
        phone_number: str,
        chat_id: str,
        timestamp: Optional[str] = None,
    ) -> "TargetResult":
        """A sent tenant-admin notification.

        ``target_id``/``campaign_id`` stay empty so the Go orchestrator stores
        the admin ``chat_id`` in ``admin_chat_phone_mappings`` for this account.
        """
        return cls(
            target_id="",
            campaign_id="",
            tenant_id=tenant_id,
            tenant_account_id=tenant_account_id,
            account_id=tenant_account_id,
            phone_number=phone_number,
            status=STATUS_SENT,
            timestamp=timestamp or utc_now_iso(),
            chat_id=chat_id,
            messenger_type=MESSENGER_TYPE_MAX,
        )
