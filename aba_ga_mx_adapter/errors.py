"""Statuses, error codes and typed exceptions for the adapter.

Statuses and the Go-known error codes mirror the Go orchestrator sources:
``internal/domain/models.go`` (TaskStatus) and ``internal/domain/errors.go``
(structured error codes and their retry disposition).

Adapter-internal diagnostic codes are *unknown* to the Go orchestrator. Go's
``ClassifyErrorCode`` treats unknown structured codes as final, which is the
intended behavior for every diagnostic failure raised here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Task / result statuses (exactly the values usable by the Go ResultConsumer)
# ---------------------------------------------------------------------------

STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_USER_NOT_FOUND_BY_PHONE = "user_not_found_by_phone"
STATUS_DELIVERY_UNKNOWN = "delivery_unknown"
# Inbound / delivery-status results (same values as the Go TaskStatus enum).
STATUS_DELIVERED = "delivered"
STATUS_VIEWED = "viewed"
STATUS_REPLIED = "replied"

# ---------------------------------------------------------------------------
# Error codes already known to the Go orchestrator (internal/domain/errors.go).
# Retry disposition is decided by Go:
#   USER_NOT_FOUND_BY_PHONE, INVALID_PHONE, ACCOUNT_MISMATCH -> final
#   SESSION_EXPIRED, WORKER_UNAVAILABLE                     -> retryable
#   DELIVERY_UNKNOWN                                        -> unknown (no retry)
# ---------------------------------------------------------------------------

ERROR_CODE_USER_NOT_FOUND_BY_PHONE = "USER_NOT_FOUND_BY_PHONE"
ERROR_CODE_INVALID_PHONE = "INVALID_PHONE"
ERROR_CODE_SESSION_EXPIRED = "SESSION_EXPIRED"
ERROR_CODE_WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
ERROR_CODE_DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
ERROR_CODE_ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"

# ---------------------------------------------------------------------------
# Adapter-internal diagnostic codes. Unknown to Go -> treated as final.
# ---------------------------------------------------------------------------

ERROR_CODE_UNSUPPORTED_ATTACHMENT = "UNSUPPORTED_ATTACHMENT"
ERROR_CODE_CHECK_ACCOUNT_RATE_LIMITED = "CHECK_ACCOUNT_RATE_LIMITED"
ERROR_CODE_UNSUPPORTED_MESSENGER_TYPE = "UNSUPPORTED_MESSENGER_TYPE"
ERROR_CODE_INVALID_MESSAGE_TEXT = "INVALID_MESSAGE_TEXT"
ERROR_CODE_MISSING_CHAT_ID = "MISSING_CHAT_ID"
ERROR_CODE_INSTANCE_BLOCKED = "INSTANCE_BLOCKED"
ERROR_CODE_INSTANCE_SUSPENDED = "INSTANCE_SUSPENDED"
ERROR_CODE_UNKNOWN_INSTANCE_STATE = "UNKNOWN_INSTANCE_STATE"
ERROR_CODE_GREEN_API_ERROR = "GREEN_API_ERROR"
ERROR_CODE_SEND_MESSAGE_REJECTED = "SEND_MESSAGE_REJECTED"
ERROR_CODE_INTERNAL_ERROR = "INTERNAL_ERROR"

# The MAX messenger type used by the Go orchestrator (domain.DefaultMessengerType).
MESSENGER_TYPE_MAX = "MAX"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


class InvalidTaskError(Exception):
    """Structurally broken task that cannot be turned into a result.

    These are published to the account dead-letter queue (with a diagnostic
    header) instead of the results queue, so a broken message cannot create an
    endless redelivery loop.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdapterError(Exception):
    """Base class for failures that map onto a TargetResult.

    ``message`` is always secret-safe: it never contains the GREEN-API token,
    a full request URL, or the full user message text.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        status: str = STATUS_FAILED,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.task_status = status


class TaskValidationError(AdapterError):
    """Task-level validation failure (final; no GREEN-API call was made)."""


class AccountMismatchError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_ACCOUNT_MISMATCH)


class UnsupportedMessengerTypeError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_UNSUPPORTED_MESSENGER_TYPE)


class UnsupportedAttachmentError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_UNSUPPORTED_ATTACHMENT)


class InvalidMessageTextError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_INVALID_MESSAGE_TEXT)


class MissingChatIdError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_MISSING_CHAT_ID)


class InvalidPhoneError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_INVALID_PHONE)


class MessageTooLongError(TaskValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_INVALID_MESSAGE_TEXT)


class GreenApiError(AdapterError):
    """GREEN-API returned an error response or an unusable response body."""

    def __init__(self, message: str, *, error_code: str = ERROR_CODE_GREEN_API_ERROR) -> None:
        super().__init__(message, error_code=error_code)


class SessionExpiredError(GreenApiError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_SESSION_EXPIRED)


class WorkerUnavailableError(GreenApiError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_WORKER_UNAVAILABLE)


class DeliveryUnknownError(GreenApiError):
    """SendMessage outcome is unknown: the message may or may not have been sent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_DELIVERY_UNKNOWN)
        self.task_status = STATUS_DELIVERY_UNKNOWN


class CheckAccountRateLimitedError(GreenApiError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_CHECK_ACCOUNT_RATE_LIMITED)


class SendMessageRejectedError(GreenApiError):
    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=ERROR_CODE_SEND_MESSAGE_REJECTED)


class UserNotFoundByPhoneError(AdapterError):
    """Recipient has no MAX account on this instance."""

    def __init__(self, message: str = "user not found by phone") -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_USER_NOT_FOUND_BY_PHONE,
            status=STATUS_USER_NOT_FOUND_BY_PHONE,
        )
