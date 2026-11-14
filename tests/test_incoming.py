"""Incoming GREEN-API notification -> result mapping tests."""

from __future__ import annotations

from aba_ga_mx_adapter.errors import STATUS_DELIVERED, STATUS_REPLIED, STATUS_VIEWED
from aba_ga_mx_adapter.incoming import notification_to_results

TENANT_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"


def _incoming(*, chat_id="155395237", text="Привет, я ответил!", chat_type="user",
              type_message="textMessage", timestamp=1763115112):
    return {
        "receiptId": 10,
        "body": {
            "typeWebhook": "incomingMessageReceived",
            "instanceData": {"idInstance": 3100000000, "typeInstance": "v3"},
            "timestamp": timestamp,
            "idMessage": "1763115112345",
            "senderData": {
                "chatId": chat_id,
                "chatType": chat_type,
                "sender": chat_id,
                "senderName": "Ходабрыш",
                "senderPhoneNumber": 79876543210,
            },
            "messageData": {
                "typeMessage": type_message,
                "textMessageData": {"textMessage": text},
            },
        },
    }


def _status(*, chat_id="155395237", status="delivered", timestamp=1763115112):
    return {
        "receiptId": 11,
        "body": {
            "typeWebhook": "outgoingMessageStatus",
            "timestamp": timestamp,
            "chatId": chat_id,
            "idMessage": "1763115112345",
            "status": status,
        },
    }


def test_incoming_text_reply_maps_to_replied():
    results = notification_to_results(_incoming(), tenant_account_id=TENANT_ACCOUNT_ID)
    assert len(results) == 1
    result = results[0]
    assert result.status == STATUS_REPLIED
    assert result.reply_text == "Привет, я ответил!"
    assert result.chat_id == "155395237"
    assert result.target_id == ""
    assert result.campaign_id == ""
    assert result.phone_number == ""
    assert result.tenant_account_id == TENANT_ACCOUNT_ID
    assert result.account_id == TENANT_ACCOUNT_ID
    assert result.messenger_type == "MAX"
    assert result.timestamp.endswith("Z")


def test_incoming_group_message_is_ignored():
    notification = _incoming(chat_type="group", chat_id="-69876543210123")
    assert notification_to_results(notification, tenant_account_id=TENANT_ACCOUNT_ID) == []


def test_incoming_non_text_message_is_ignored():
    notification = _incoming(type_message="imageMessage")
    assert notification_to_results(notification, tenant_account_id=TENANT_ACCOUNT_ID) == []


def test_incoming_empty_text_is_ignored():
    assert notification_to_results(
        _incoming(text=""), tenant_account_id=TENANT_ACCOUNT_ID
    ) == []


def test_own_notification_echo_is_ignored():
    notification = _incoming(text="🔔 Получены новые сообщения:\n1. ...")
    assert notification_to_results(notification, tenant_account_id=TENANT_ACCOUNT_ID) == []


def test_status_delivered_maps_to_delivered():
    results = notification_to_results(_status(status="delivered"), tenant_account_id=TENANT_ACCOUNT_ID)
    assert len(results) == 1
    assert results[0].status == STATUS_DELIVERED
    assert results[0].chat_id == "155395237"
    assert results[0].reply_text is None


def test_status_read_maps_to_viewed():
    results = notification_to_results(_status(status="read"), tenant_account_id=TENANT_ACCOUNT_ID)
    assert len(results) == 1
    assert results[0].status == STATUS_VIEWED


def test_unknown_status_and_type_are_ignored():
    assert notification_to_results(
        _status(status="sent"), tenant_account_id=TENANT_ACCOUNT_ID
    ) == []
    assert notification_to_results(
        {"body": {"typeWebhook": "stateInstanceChanged"}},
        tenant_account_id=TENANT_ACCOUNT_ID,
    ) == []
    assert notification_to_results({}, tenant_account_id=TENANT_ACCOUNT_ID) == []
    assert notification_to_results("nonsense", tenant_account_id=TENANT_ACCOUNT_ID) == []


def test_reply_json_shape_matches_go_result():
    result = notification_to_results(_incoming(), tenant_account_id=TENANT_ACCOUNT_ID)[0]
    data = result.to_dict()
    assert data["status"] == "replied"
    assert data["reply_text"] == "Привет, я ответил!"
    assert data["target_id"] == ""
    assert "provider_message_id" not in data
