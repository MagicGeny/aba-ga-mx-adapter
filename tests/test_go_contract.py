"""Compatibility checks against the Go orchestrator contract.

These tests hard-code the expectations from the Go sources
(``internal/domain/models.go`` and ``internal/domain/errors.go``) so the
adapter's wire format cannot silently drift.
"""

from __future__ import annotations

import json
from datetime import datetime

from aba_ga_mx_adapter.errors import (
    ERROR_CODE_ACCOUNT_MISMATCH,
    ERROR_CODE_CHECK_ACCOUNT_RATE_LIMITED,
    ERROR_CODE_DELIVERY_UNKNOWN,
    ERROR_CODE_GREEN_API_ERROR,
    ERROR_CODE_INSTANCE_BLOCKED,
    ERROR_CODE_INSTANCE_SUSPENDED,
    ERROR_CODE_INTERNAL_ERROR,
    ERROR_CODE_INVALID_MESSAGE_TEXT,
    ERROR_CODE_INVALID_PHONE,
    ERROR_CODE_MISSING_CHAT_ID,
    ERROR_CODE_SEND_MESSAGE_REJECTED,
    ERROR_CODE_SESSION_EXPIRED,
    ERROR_CODE_UNKNOWN_INSTANCE_STATE,
    ERROR_CODE_UNSUPPORTED_ATTACHMENT,
    ERROR_CODE_UNSUPPORTED_MESSENGER_TYPE,
    ERROR_CODE_USER_NOT_FOUND_BY_PHONE,
    ERROR_CODE_WORKER_UNAVAILABLE,
)
from aba_ga_mx_adapter.models import SendTaskPayload, TargetResult

from tests.adapter_test_helpers import CHAT_ID, task_bytes

# Go TaskStatus constants (internal/domain/models.go).
GO_TASK_STATUSES = {
    "pending",
    "sent",
    "delivered",
    "viewed",
    "failed",
    "replied",
    "user_not_found_by_phone",
    "retry_pending",
    "delivery_unknown",
}

# Go error codes (internal/domain/errors.go).
GO_ERROR_CODES = {
    "USER_NOT_FOUND_BY_PHONE",
    "INVALID_PHONE",
    "SESSION_EXPIRED",
    "WORKER_UNAVAILABLE",
    "MAX_UI_ERROR",
    "BROWSER_ERROR",
    "DELIVERY_UNKNOWN",
    "BLOCKED_RECIPIENT",
    "ACCOUNT_MISMATCH",
}

# Adapter-internal diagnostic codes (final for Go).
ADAPTER_ERROR_CODES = {
    "UNSUPPORTED_ATTACHMENT",
    "CHECK_ACCOUNT_RATE_LIMITED",
    "UNSUPPORTED_MESSENGER_TYPE",
    "INVALID_MESSAGE_TEXT",
    "MISSING_CHAT_ID",
    "INSTANCE_BLOCKED",
    "INSTANCE_SUSPENDED",
    "UNKNOWN_INSTANCE_STATE",
    "GREEN_API_ERROR",
    "SEND_MESSAGE_REJECTED",
    "INTERNAL_ERROR",
}

# Exact set of JSON keys the Go ResultConsumer can accept for a worker result.
GO_RESULT_KEYS = {
    "target_id",
    "campaign_id",
    "tenant_id",
    "tenant_account_id",
    "account_id",
    "phone_number",
    "status",
    "reply_text",
    "error_code",
    "error_message",
    "timestamp",
    "chat_id",
    "messenger_type",
}

FIXED_TIMESTAMP = "2026-11-12T12:00:00.000Z"


def _fixed_result() -> TargetResult:
    task = SendTaskPayload.from_json(task_bytes())
    return TargetResult.for_task(
        task,
        status="sent",
        chat_id=CHAT_ID,
        timestamp=FIXED_TIMESTAMP,
    )


def test_sent_result_serializes_to_exact_go_shape():
    result = _fixed_result()
    assert result.to_dict() == {
        "target_id": "01234567-89ab-cdef-0123-456789abcdef",
        "campaign_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tenant_id": "99999999-8888-7777-6666-555555555555",
        "tenant_account_id": "11111111-2222-3333-4444-555555555555",
        "account_id": "11111111-2222-3333-4444-555555555555",
        "phone_number": "79991234567",
        "status": "sent",
        "error_code": "",
        "error_message": None,
        "timestamp": FIXED_TIMESTAMP,
        "chat_id": "10000000",
        "messenger_type": "MAX",
    }


def test_result_keys_are_subset_of_go_target_result():
    result = _fixed_result().to_dict()
    assert set(result) <= GO_RESULT_KEYS


def test_result_does_not_include_reply_text_or_provider_message_id():
    data = _fixed_result().to_dict()
    assert "reply_text" not in data
    assert "provider_message_id" not in data


def test_statuses_are_go_task_statuses():
    """Every status the adapter can emit exists in the Go enum."""
    for status in ("sent", "failed", "user_not_found_by_phone", "delivery_unknown"):
        assert status in GO_TASK_STATUSES


def test_all_error_codes_are_known_or_documented():
    codes = {
        ERROR_CODE_USER_NOT_FOUND_BY_PHONE,
        ERROR_CODE_INVALID_PHONE,
        ERROR_CODE_SESSION_EXPIRED,
        ERROR_CODE_WORKER_UNAVAILABLE,
        ERROR_CODE_DELIVERY_UNKNOWN,
        ERROR_CODE_ACCOUNT_MISMATCH,
        ERROR_CODE_UNSUPPORTED_ATTACHMENT,
        ERROR_CODE_CHECK_ACCOUNT_RATE_LIMITED,
        ERROR_CODE_UNSUPPORTED_MESSENGER_TYPE,
        ERROR_CODE_INVALID_MESSAGE_TEXT,
        ERROR_CODE_MISSING_CHAT_ID,
        ERROR_CODE_INSTANCE_BLOCKED,
        ERROR_CODE_INSTANCE_SUSPENDED,
        ERROR_CODE_UNKNOWN_INSTANCE_STATE,
        ERROR_CODE_GREEN_API_ERROR,
        ERROR_CODE_SEND_MESSAGE_REJECTED,
        ERROR_CODE_INTERNAL_ERROR,
    }
    assert codes <= (GO_ERROR_CODES | ADAPTER_ERROR_CODES)


def test_timestamp_is_rfc3339_utc():
    timestamp = _fixed_result().timestamp
    assert timestamp.endswith("Z")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


def test_account_id_is_sender_account_not_id_instance():
    """account_id must be the tenant_accounts.id UUID, never the GREEN-API idInstance."""
    result = _fixed_result().to_dict()
    assert result["account_id"] == result["tenant_account_id"]
    # GREEN-API idInstance values (e.g. 1101000001) never appear as account_id.
    assert result["account_id"] != "1101000001"


def test_serialized_json_round_trips_like_go():
    raw = _fixed_result().to_json()
    parsed = json.loads(raw)
    assert set(parsed) <= GO_RESULT_KEYS
    # Go's TargetResult.UnmarshalJSON parses these as UUID strings.
    for key in ("target_id", "campaign_id", "tenant_id", "tenant_account_id"):
        assert len(parsed[key]) == 36


def test_replied_result_shape_matches_go():
    """An inbound reply: orchestrator resolves target by chat_id."""
    result = TargetResult.for_incoming_reply(
        tenant_account_id="11111111-2222-3333-4444-555555555555",
        chat_id="155395237",
        reply_text="да, актуально",
        timestamp=FIXED_TIMESTAMP,
    )
    data = result.to_dict()
    assert data == {
        "target_id": "",
        "campaign_id": "",
        "tenant_id": "",
        "tenant_account_id": "11111111-2222-3333-4444-555555555555",
        "account_id": "11111111-2222-3333-4444-555555555555",
        "phone_number": "",
        "status": "replied",
        "error_code": "",
        "error_message": None,
        "timestamp": FIXED_TIMESTAMP,
        "chat_id": "155395237",
        "messenger_type": "MAX",
        "reply_text": "да, актуально",
    }
    assert set(data) <= GO_RESULT_KEYS


def test_admin_notify_result_has_no_campaign_target():
    result = TargetResult.for_admin_notify_sent(
        tenant_id="99999999-8888-7777-6666-555555555555",
        tenant_account_id="11111111-2222-3333-4444-555555555555",
        phone_number="9991234567",
        chat_id="155395237",
        timestamp=FIXED_TIMESTAMP,
    )
    data = result.to_dict()
    assert data["target_id"] == ""
    assert data["campaign_id"] == ""
    assert data["status"] == "sent"
    assert data["phone_number"] == "9991234567"
    assert "reply_text" not in data
    assert set(data) <= GO_RESULT_KEYS
