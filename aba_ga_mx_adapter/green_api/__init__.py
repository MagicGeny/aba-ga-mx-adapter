"""GREEN-API MAX HTTP client package."""

from .client import (
    CHECK_ACCOUNT_COOLDOWN_SECONDS,
    GO_CANONICAL_PHONE_REGEX,
    GREEN_API_PHONE_REGEX,
    MAX_MESSAGE_LENGTH,
    RECEIVE_TIMEOUT_DEFAULT,
    RECEIVE_TIMEOUT_MAX,
    RECEIVE_TIMEOUT_MIN,
    CheckAccountResult,
    GetStateInstance,
    GreenApiMaxClient,
    SendMessageResult,
    require_authorized,
    to_go_canonical_phone,
    to_green_api_phone,
)

__all__ = [
    "CHECK_ACCOUNT_COOLDOWN_SECONDS",
    "GO_CANONICAL_PHONE_REGEX",
    "GREEN_API_PHONE_REGEX",
    "MAX_MESSAGE_LENGTH",
    "RECEIVE_TIMEOUT_DEFAULT",
    "RECEIVE_TIMEOUT_MAX",
    "RECEIVE_TIMEOUT_MIN",
    "CheckAccountResult",
    "GetStateInstance",
    "GreenApiMaxClient",
    "SendMessageResult",
    "require_authorized",
    "to_go_canonical_phone",
    "to_green_api_phone",
]
