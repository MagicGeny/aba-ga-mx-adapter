"""Tenant-admin notification task model and rendering.

The Go orchestrator, after registering an inbound reply, publishes a
``TenantAdminNotificationTask`` to ``tasks.messages.tenant_admin_notify.account.
<tenant_account_id>``. This module parses it and renders the human-readable
text (the same layout the Playwright adapter used).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    MSK_TZ = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - tz database missing
    MSK_TZ = timezone(timedelta(hours=3), name="Europe/Moscow")

from .errors import InvalidTaskError

NOTIFICATION_PREFIX = "🔔 Получены новые сообщения:"


@dataclass(frozen=True)
class AdminNotificationReply:
    user_phone: str
    user_name: str
    message: str
    time: str


@dataclass(frozen=True)
class AdminNotificationTask:
    tenant_phone: str
    tenant_id: str
    tenant_account_id: str
    chat_id: str
    use_chat_id: bool
    replies: list[AdminNotificationReply] = field(default_factory=list)


def parse_admin_notification_task(body: Any) -> AdminNotificationTask:
    """Parse a raw notification-task dict. Raises :class:`InvalidTaskError`."""
    if not isinstance(body, dict):
        raise InvalidTaskError("notify_payload_is_not_an_object")

    tenant_phone = str(body.get("tenant_phone") or "").strip()
    if not tenant_phone:
        raise InvalidTaskError("notify_payload_missing_tenant_phone")

    raw_replies = body.get("replies")
    if not isinstance(raw_replies, list):
        raise InvalidTaskError("notify_payload_missing_replies")

    replies: list[AdminNotificationReply] = []
    for item in raw_replies:
        if not isinstance(item, dict):
            continue
        replies.append(
            AdminNotificationReply(
                user_phone=str(item.get("user_phone") or ""),
                user_name=str(item.get("user_name") or ""),
                message=str(item.get("message") or ""),
                time=str(item.get("time") or ""),
            )
        )

    return AdminNotificationTask(
        tenant_phone=tenant_phone,
        tenant_id=str(body.get("tenant_id") or ""),
        tenant_account_id=str(body.get("tenant_account_id") or ""),
        chat_id=str(body.get("chat_id") or ""),
        use_chat_id=bool(body.get("use_chat_id")),
        replies=replies,
    )


def _to_moscow_time(raw: str) -> datetime:
    """Parse an RFC3339/ISO timestamp (assume UTC when naive) into MSK."""
    value = (raw or "").strip()
    parsed: Optional[datetime] = None
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MSK_TZ)


def format_notification_message(task: AdminNotificationTask) -> str:
    """Render the notification text sent to the tenant admin."""
    lines = [NOTIFICATION_PREFIX]
    for index, reply in enumerate(task.replies, start=1):
        time_str = _to_moscow_time(reply.time).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"\n{index}. 📱 {reply.user_phone} ({reply.user_name})")
        lines.append(f"   💬 Ответ: {reply.message}")
        lines.append(f"   🕒 Время: {time_str}")
    return "\n".join(lines)
