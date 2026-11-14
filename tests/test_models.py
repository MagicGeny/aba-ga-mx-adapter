"""Tests for SendTaskPayload parsing and TargetResult serialization."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from aba_ga_mx_adapter.errors import (
    InvalidTaskError,
    MESSENGER_TYPE_MAX,
    STATUS_DELIVERY_UNKNOWN,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_USER_NOT_FOUND_BY_PHONE,
)
from aba_ga_mx_adapter.models import (
    RESULT_FIELDS,
    SendTaskPayload,
    TargetResult,
)

from tests.adapter_test_helpers import (
    CAMPAIGN_ID,
    CHAT_ID,
    TASK_ID,
    TENANT_ACCOUNT_ID,
    TENANT_ID,
    task_bytes,
    task_dict,
)


def test_parse_valid_payload():
    task = SendTaskPayload.from_json(task_bytes())
    assert task.task_id == TASK_ID
    assert task.campaign_id == CAMPAIGN_ID
    assert task.tenant_id == TENANT_ID
    assert task.tenant_account_id == TENANT_ACCOUNT_ID
    assert task.account_id == TENANT_ACCOUNT_ID
    assert task.messenger_type == "MAX"
    assert task.messenger == "max"
    assert task.use_chat_id is False


def test_task_id_becomes_target_id():
    task = SendTaskPayload.from_json(task_bytes())
    assert task.target_id == task.task_id
    result = TargetResult.sent(task, chat_id=CHAT_ID)
    assert result.target_id == TASK_ID


@pytest.mark.parametrize(
    "field",
    ["task_id", "campaign_id", "tenant_id", "tenant_account_id"],
)
def test_invalid_uuid_is_structurally_invalid(field):
    payload = task_dict(**{field: "not-a-uuid"})
    with pytest.raises(InvalidTaskError):
        SendTaskPayload.from_json(json.dumps(payload))


@pytest.mark.parametrize(
    "field",
    ["task_id", "campaign_id", "tenant_id", "tenant_account_id"],
)
def test_missing_uuid_is_structurally_invalid(field):
    payload = task_dict()
    payload.pop(field)
    with pytest.raises(InvalidTaskError):
        SendTaskPayload.from_json(json.dumps(payload))


def test_invalid_json_is_structurally_invalid():
    with pytest.raises(InvalidTaskError):
        SendTaskPayload.from_json(b"{not json")


def test_non_object_payload_is_structurally_invalid():
    with pytest.raises(InvalidTaskError):
        SendTaskPayload.from_json(b"[1, 2, 3]")


def test_non_boolean_use_chat_id_is_structurally_invalid():
    payload = task_dict(use_chat_id="true")
    with pytest.raises(InvalidTaskError):
        SendTaskPayload.from_json(json.dumps(payload))


def test_uuid_case_is_canonicalized():
    payload = task_dict(task_id=TASK_ID.upper())
    task = SendTaskPayload.from_json(json.dumps(payload))
    assert task.task_id == TASK_ID.lower()


def test_attachments_detected():
    assert SendTaskPayload.from_json(task_bytes()).has_attachment() is False
    assert (
        SendTaskPayload.from_json(
            task_bytes(attachment_url="https://example.com/file.pdf")
        ).has_attachment()
        is True
    )
    assert (
        SendTaskPayload.from_json(
            task_bytes(attachment_name="file.pdf")
        ).has_attachment()
        is True
    )
    # Whitespace-only attachment values are treated as absent.
    assert (
        SendTaskPayload.from_json(
            task_bytes(attachment_url="   ")
        ).has_attachment()
        is False
    )


def test_result_has_exact_go_fields():
    result = TargetResult.for_task(
        SendTaskPayload.from_json(task_bytes()),
        status=STATUS_SENT,
        chat_id=CHAT_ID,
    )
    data = result.to_dict()
    assert set(data) == set(RESULT_FIELDS)


def test_result_never_contains_provider_message_id():
    result = TargetResult.sent(SendTaskPayload.from_json(task_bytes()), chat_id=CHAT_ID)
    assert "provider_message_id" not in result.to_dict()
    assert b"provider_message_id" not in result.to_json()


def test_result_json_payload_is_utf8_and_stable():
    result = TargetResult.sent(SendTaskPayload.from_json(task_bytes()), chat_id=CHAT_ID)
    parsed = json.loads(result.to_json().decode("utf-8"))
    assert parsed["status"] == "sent"
    assert parsed["messenger_type"] == "MAX"
    assert parsed["chat_id"] == CHAT_ID
    assert parsed["error_code"] == ""
    assert parsed["error_message"] is None


@pytest.mark.parametrize(
    "status",
    [STATUS_SENT, STATUS_FAILED, STATUS_USER_NOT_FOUND_BY_PHONE, STATUS_DELIVERY_UNKNOWN],
)
def test_statuses_are_go_constants(status):
    result = TargetResult.for_task(
        SendTaskPayload.from_json(task_bytes()), status=status
    )
    assert result.status == status


def test_timestamp_is_iso8601_utc():
    result = TargetResult.sent(SendTaskPayload.from_json(task_bytes()), chat_id=CHAT_ID)
    timestamp = result.timestamp
    assert timestamp.endswith("Z")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_all_uuids_present_as_strings():
    result = TargetResult.for_task(
        SendTaskPayload.from_json(task_bytes()), status=STATUS_FAILED
    )
    data = result.to_dict()
    for key in ("target_id", "campaign_id", "tenant_id", "tenant_account_id"):
        assert isinstance(data[key], str)
        assert len(data[key]) == 36
    assert data["account_id"] == TENANT_ACCOUNT_ID
    assert data["messenger_type"] == MESSENGER_TYPE_MAX
