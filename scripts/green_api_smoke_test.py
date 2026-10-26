#!/usr/bin/env python3
"""GREEN-API MAX smoke test.

Verifies the following sequence against a real GREEN-API MAX instance:

    CheckAccount -> SendMessage -> ReceiveNotification -> outgoingMessageStatus: read

Usage:

    python scripts/green_api_smoke_test.py --check-only
    python scripts/green_api_smoke_test.py --send-and-wait-read

Before running:

    1. Copy .env.example to .env and fill in your GREEN-API credentials.
    2. Ensure the MAX instance has:
       - webhookUrl cleared (empty)
       - outgoingMessageWebhook, outgoingAPIMessageWebhook, outgoingWebhook enabled
    3. Use a dedicated test instance with an empty notification queue.

This script NEVER sends messages automatically. You must explicitly pass a
mode and, for full runs, confirm by typing SEND.

See docs/adapter-contract.md for the full adapter contract.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)

GREEN_API_BASE_URL = os.getenv("GREEN_API_BASE_URL", "").strip()
GREEN_API_ID_INSTANCE = os.getenv("GREEN_API_ID_INSTANCE", "").strip()
GREEN_API_TOKEN_INSTANCE = os.getenv("GREEN_API_TOKEN_INSTANCE", "").strip()
TEST_PHONE_NUMBER = os.getenv("TEST_PHONE_NUMBER", "").strip()
TEST_MESSAGE = os.getenv("TEST_MESSAGE", "").strip()
SMOKE_TEST_TIMEOUT_SECONDS = int(
    os.getenv("SMOKE_TEST_TIMEOUT_SECONDS", "180").strip()
)

# GREEN-API ReceiveNotification receiveTimeout range (seconds).
# See: https://green-api.com/v3/docs/api/receiving/technology-http-api/ReceiveNotification/
RECEIVE_TIMEOUT_MIN = 5
RECEIVE_TIMEOUT_MAX = 60
RECEIVE_TIMEOUT_DEFAULT = 10

# The generic HTTP client timeout (HTTP_TIMEOUT_DEFAULT) is intentionally
# shorter than the longest allowed long-poll (RECEIVE_TIMEOUT_MAX). Each
# ReceiveNotification request therefore overrides it with
# receiveTimeout + RECEIVE_TIMEOUT_MARGIN_SECONDS, so a server-side long poll
# is never cut off by the shorter generic timeout.
HTTP_TIMEOUT_DEFAULT = 30.0
RECEIVE_TIMEOUT_MARGIN_SECONDS = 15.0

# GREEN-API MAX CheckAccount phone requirements:
#   * digits only (no '+', spaces, or separators);
#   * 11 digits beginning with '7' for Russian numbers;
#   * 12 digits beginning with '375' for Belarusian numbers.
# See: https://green-api.com/v3/docs/api/service/CheckAccount/
PHONE_REGEX = re.compile(r"^(?:7\d{10}|375\d{9})$")

# SendMessage text length limit enforced by GREEN-API MAX.
MAX_TEST_MESSAGE_LENGTH = 4000

# Terminal statuses from OutgoingMessageStatus notifications.
TERMINAL_STATUSES = {"read", "failed", "noAccount", "notInGroup"}

# Execution modes.
MODE_CHECK_ONLY = "check_only"
MODE_SEND_AND_WAIT_READ = "send_and_wait_read"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Print a log line with a UTC timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _log_error(msg: str) -> None:
    """Print an error log line."""
    _log(f"ERROR: {msg}")


def _log_warn(msg: str) -> None:
    """Print a warning log line."""
    _log(f"WARNING: {msg}")


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes from the base URL."""
    return base_url.rstrip("/")


# The GREEN-API token is the path segment immediately after the method name.
# For deleteNotification the receiptId follows the token, so everything after
# the method name must be masked, not just the last path segment.
_SENSITIVE_PATH_RE = re.compile(
    r"/(checkAccount|sendMessage|receiveNotification|deleteNotification)"
    r"(?:/[^\s?]*)?(?:\?[^\s]*)?$"
)


def _sanitize_url(url: str) -> str:
    """Return a URL with the token portion masked for logging.

    GREEN-API places the API token in the URL path. For ``deleteNotification``
    the ``receiptId`` follows the token, so the whole tail after the method
    name is masked. We must never print the token.
    """
    return _SENSITIVE_PATH_RE.sub(r"/\1/<hidden>", url)


# ---------------------------------------------------------------------------
# Data models (lightweight, no pydantic dependency needed for smoke test)
# ---------------------------------------------------------------------------

@dataclass
class CheckAccountResult:
    """Result of a CheckAccount API call."""
    exist: bool
    chat_id: Optional[str]
    from_cache: Optional[bool]
    raw_response: dict[str, Any]

    @property
    def has_valid_chat(self) -> bool:
        return self.exist and self.chat_id is not None and self.chat_id != ""


@dataclass
class SendMessageResult:
    """Result of a SendMessage API call."""
    id_message: str
    raw_response: dict[str, Any]


@dataclass
class Notification:
    """A notification returned by ReceiveNotification."""
    receipt_id: Optional[int]
    type_webhook: Optional[str]
    id_message: Optional[str]
    status: Optional[str]
    chat_id: Optional[str]
    description: Optional[str]
    timestamp: Optional[int]
    raw_body: dict[str, Any]

    @classmethod
    def from_raw(cls, notification: dict[str, Any]) -> "Notification":
        """Parse a ReceiveNotification item into a Notification object."""
        body = notification.get("body", {})
        if isinstance(body, dict):
            return cls(
                receipt_id=notification.get("receiptId"),
                type_webhook=body.get("typeWebhook"),
                id_message=body.get("idMessage"),
                status=body.get("status"),
                chat_id=body.get("chatId"),
                description=body.get("description"),
                timestamp=body.get("timestamp"),
                raw_body=dict(body),
            )
        return cls(
            receipt_id=notification.get("receiptId"),
            type_webhook=None,
            id_message=None,
            status=None,
            chat_id=None,
            description=None,
            timestamp=None,
            raw_body=body if isinstance(body, dict) else {},
        )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def validate_config(mode: str = MODE_SEND_AND_WAIT_READ) -> None:
    """Validate that all required configuration is present and correct.

    Validation is mode-aware:

    * ``MODE_CHECK_ONLY`` only needs the instance credentials and a valid test
      phone number. It never sends, so ``TEST_MESSAGE`` is not required.
    * ``MODE_SEND_AND_WAIT_READ`` additionally requires a non-empty test
      message no longer than ``MAX_TEST_MESSAGE_LENGTH`` characters.

    Does NOT make any API calls.
    """
    if mode not in (MODE_CHECK_ONLY, MODE_SEND_AND_WAIT_READ):
        raise ConfigError(f"Unknown mode: {mode!r}")

    missing = []
    if not GREEN_API_BASE_URL:
        missing.append("GREEN_API_BASE_URL")
    if not GREEN_API_ID_INSTANCE:
        missing.append("GREEN_API_ID_INSTANCE")
    if not GREEN_API_TOKEN_INSTANCE:
        missing.append("GREEN_API_TOKEN_INSTANCE")
    if not TEST_PHONE_NUMBER:
        missing.append("TEST_PHONE_NUMBER")
    if mode == MODE_SEND_AND_WAIT_READ and not TEST_MESSAGE:
        missing.append("TEST_MESSAGE")

    if missing:
        raise ConfigError(
            f"Missing required configuration: {', '.join(missing)}.\n"
            f"Copy .env.example to .env and fill in all values."
        )

    if not GREEN_API_BASE_URL.startswith(("http://", "https://")):
        raise ConfigError(
            "GREEN_API_BASE_URL must start with http:// or https://"
        )

    # GREEN-API MAX CheckAccount accepts only digits: 11 digits beginning with
    # '7' (RU) or 12 digits beginning with '375' (BY).
    if not PHONE_REGEX.match(TEST_PHONE_NUMBER):
        raise ConfigError(
            "TEST_PHONE_NUMBER must be digits only, either 11 digits starting "
            "with '7' (RU) or 12 digits starting with '375' (BY), without '+' "
            f"or separators: got {TEST_PHONE_NUMBER!r}"
        )

    if mode == MODE_SEND_AND_WAIT_READ:
        if len(TEST_MESSAGE) > MAX_TEST_MESSAGE_LENGTH:
            raise ConfigError(
                f"TEST_MESSAGE must be at most {MAX_TEST_MESSAGE_LENGTH} "
                f"characters: got {len(TEST_MESSAGE)}."
            )


# ---------------------------------------------------------------------------
# API error handling helpers
# ---------------------------------------------------------------------------

def _safe_error_detail(operation: str, exc: BaseException) -> str:
    """Build a secret-safe error description for a failed HTTP request.

    Only the operation name and the exception *type* are used. The raw
    exception string is deliberately excluded because HTTPX embeds the full
    request URL (including the API token) in its messages.
    """
    return f"{operation}: request failed ({type(exc).__name__})."


def _safe_parse_json(resp: httpx.Response, context: str) -> Any:
    """Parse JSON safely, without ever logging the response body."""
    try:
        return resp.json()
    except Exception:
        content_type = resp.headers.get("content-type", "unknown")
        raise ConfigError(
            f"{context}: failed to parse JSON response. "
            f"HTTP {resp.status_code}. Content-Type: {content_type}."
        )


def _raise_api_error(operation: str, resp: httpx.Response) -> None:
    """Raise a ConfigError with a safe diagnostic from an API error response.

    Never logs the token, the full URL, or the complete response body. Only
    the HTTP status and GREEN-API's structured ``code``/``message`` fields are
    used, and the message is truncated.
    """
    try:
        data = resp.json()
    except Exception:
        data = None

    error_code = None
    error_message = None
    if isinstance(data, dict):
        error_code = data.get("code") or data.get("error_code")
        error_message = data.get("message") or data.get("error_message")
        if error_message is None:
            error_message = data.get("reason")

    status = resp.status_code

    if status == 401:
        detail = "Authorization failed (invalid idInstance or apiTokenInstance)."
    elif status == 429:
        detail = "Rate limited by GREEN-API. Wait before retrying."
    elif status == 400:
        detail = "Bad request (HTTP 400). Check request parameters."
    elif status == 403:
        detail = "Forbidden (HTTP 403). Check instance access."
    elif status == 404:
        detail = "Not found (HTTP 404). Check GREEN_API_BASE_URL and endpoint path."
    else:
        detail = f"HTTP {status}"

    if error_message:
        detail = f"{detail} GREEN-API message: {str(error_message)[:300]}"
    if error_code:
        detail = f"{detail} GREEN-API code: {error_code}"

    raise ConfigError(f"{operation} API error: {detail}")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class GreenApiClient:
    """Minimal GREEN-API MAX HTTP client for smoke testing."""

    def __init__(self, base_url: str, id_instance: str, token: str,
                 timeout: float = HTTP_TIMEOUT_DEFAULT):
        self._base_url = _normalize_base_url(base_url)
        self._id_instance = id_instance
        self._token = token
        self._timeout = timeout
        self._client: Optional[httpx.Client] = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                base_url=self._base_url,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    def _wa_url(self, method: str) -> str:
        """Build a GREEN-API waInstance URL.

        The token is part of the path. This method does NOT sanitize.
        """
        return (
            f"{self._base_url}/waInstance{self._id_instance}"
            f"/{method}/{self._token}"
        )

    def _receive_request_timeout(self, receive_timeout: int) -> float:
        """HTTP timeout for a ReceiveNotification long-poll request.

        Longer than the selected ``receiveTimeout`` so the server-side long
        poll is never cut off by the shorter generic HTTP timeout.
        """
        return float(receive_timeout) + RECEIVE_TIMEOUT_MARGIN_SECONDS

    def check_account(self, phone_number: str, force: bool = False) -> CheckAccountResult:
        """Call CheckAccount API. See docs for details."""
        body: dict[str, Any] = {"phoneNumber": phone_number}
        if force:
            body["force"] = True

        url = self._wa_url("checkAccount")
        _log(f"CheckAccount -> {_sanitize_url(url)}")

        try:
            resp = self._get_client().post(url, json=body)
        except httpx.TimeoutException:
            raise ConfigError("CheckAccount request timed out.")
        except httpx.HTTPError as exc:
            raise ConfigError(_safe_error_detail("CheckAccount", exc))

        if resp.status_code != 200:
            _raise_api_error("CheckAccount", resp)

        data = _safe_parse_json(resp, "CheckAccount")

        if not isinstance(data, dict):
            raise ConfigError(
                f"CheckAccount returned non-object response: {type(data).__name__}"
            )

        response_copy = dict(data)
        exist = data.get("exist")
        chat_id = data.get("chatId")
        from_cache = data.get("fromCache")

        if exist is None:
            raise ConfigError(
                "CheckAccount response missing 'exist' field "
                f"(response keys: {sorted(response_copy)})."
            )

        if not exist:
            chat_id = None
            from_cache = None
            response_copy = {"exist": False}

        return CheckAccountResult(
            exist=bool(exist),
            chat_id=chat_id if isinstance(chat_id, str) else None,
            from_cache=bool(from_cache) if from_cache is not None else None,
            raw_response=response_copy,
        )

    def send_message(self, chat_id: str, message: str) -> SendMessageResult:
        """Call SendMessage API. See docs for details."""
        body = {"chatId": chat_id, "message": message}
        _log("SendMessage -> sending message (URL hidden)")

        try:
            resp = self._get_client().post(self._wa_url("sendMessage"), json=body)
        except httpx.TimeoutException:
            raise ConfigError(
                "SendMessage request timed out. The message MAY have been sent. "
                "Check the recipient's device before retrying."
            )
        except httpx.HTTPError as exc:
            raise ConfigError(_safe_error_detail("SendMessage", exc))

        if resp.status_code != 200:
            _raise_api_error("SendMessage", resp)

        data = _safe_parse_json(resp, "SendMessage")

        if not isinstance(data, dict):
            raise ConfigError(
                f"SendMessage returned non-object response: {type(data).__name__}"
            )

        id_message = data.get("idMessage")
        if not id_message or not isinstance(id_message, str):
            raise ConfigError("SendMessage response missing 'idMessage'.")

        _log(f"SendMessage accepted. idMessage={id_message}")
        return SendMessageResult(id_message=id_message, raw_response=dict(data))

    # -- ReceiveNotification / DeleteNotification ---------------------------

    def receive_notification(
        self, receive_timeout: int = RECEIVE_TIMEOUT_DEFAULT
    ) -> Optional[list[dict[str, Any]]]:
        """Call ReceiveNotification API.

        See: https://green-api.com/v3/docs/api/receiving/technology-http-api/ReceiveNotification/

        receive_timeout: 5-60 seconds (GREEN-API documented range).
        Returns the list of notifications, or None if no notifications.
        """
        if not (RECEIVE_TIMEOUT_MIN <= receive_timeout <= RECEIVE_TIMEOUT_MAX):
            receive_timeout = RECEIVE_TIMEOUT_DEFAULT

        request_timeout = self._receive_request_timeout(receive_timeout)
        _log(
            f"ReceiveNotification (receiveTimeout={receive_timeout}s, "
            f"httpTimeout={request_timeout:g}s)"
        )

        try:
            resp = self._get_client().get(
                self._wa_url("receiveNotification"),
                params={"receiveTimeout": receive_timeout},
                timeout=request_timeout,
            )
        except httpx.TimeoutException:
            # ReceiveNotification timeout just means no notification was
            # available within the window.
            return None
        except httpx.HTTPError as exc:
            raise ConfigError(_safe_error_detail("ReceiveNotification", exc))

        if resp.status_code != 200:
            _raise_api_error("ReceiveNotification", resp)

        data = _safe_parse_json(resp, "ReceiveNotification")

        if data is None:
            return None

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return None

    def delete_notification(self, receipt_id: int) -> bool:
        """Delete a processed notification.

        See: https://green-api.com/v3/docs/api/receiving/technology-http-api/DeleteNotification/

        The API expects the receipt id in the URL path, after the token:
            DELETE /waInstance{idInstance}/deleteNotification/{token}/{receiptId}
        """
        url = f"{self._wa_url('deleteNotification')}/{receipt_id}"
        _log(f"DeleteNotification -> {_sanitize_url(url)}")

        try:
            resp = self._get_client().delete(url, timeout=self._timeout)
        except httpx.TimeoutException:
            raise ConfigError("DeleteNotification request timed out.")
        except httpx.HTTPError as exc:
            raise ConfigError(_safe_error_detail("DeleteNotification", exc))

        if resp.status_code != 200:
            _raise_api_error("DeleteNotification", resp)

        data = _safe_parse_json(resp, "DeleteNotification")

        if isinstance(data, dict):
            return bool(data.get("result", True))
        return True


# ---------------------------------------------------------------------------
# Smoke-test flows
# ---------------------------------------------------------------------------

def _build_client() -> GreenApiClient:
    return GreenApiClient(
        base_url=GREEN_API_BASE_URL,
        id_instance=GREEN_API_ID_INSTANCE,
        token=GREEN_API_TOKEN_INSTANCE,
        timeout=HTTP_TIMEOUT_DEFAULT,
    )


def _confirm_send(chat_id: str, message: str) -> bool:
    """Ask the operator to explicitly confirm sending a real message."""
    _log_warn("About to send a REAL message through GREEN-API MAX.")
    print(f"  chatId: {chat_id}")
    preview = message if len(message) <= 120 else f"{message[:120]}..."
    print(f"  message ({len(message)} chars): {preview}")
    try:
        answer = input("Type SEND to confirm, or anything else to abort: ")
    except EOFError:
        answer = ""
    if answer.strip() != "SEND":
        _log("Confirmation not provided; aborting without sending.")
        return False
    return True


def run_check_only() -> int:
    """Validate configuration and run CheckAccount only. Never sends."""
    validate_config(MODE_CHECK_ONLY)
    client = _build_client()

    result = client.check_account(TEST_PHONE_NUMBER)
    if result.has_valid_chat:
        _log(f"CheckAccount OK: exist=True chatId={result.chat_id}")
        return 0

    _log_error("CheckAccount: no MAX account for the configured test phone number.")
    return 2


def run_send_and_wait_read() -> int:
    """Validate, confirm, send one message, then wait for the read status."""
    validate_config(MODE_SEND_AND_WAIT_READ)
    client = _build_client()

    check = client.check_account(TEST_PHONE_NUMBER)
    if not check.has_valid_chat:
        raise ConfigError(
            "CheckAccount found no MAX chat for the configured test phone number."
        )

    if not _confirm_send(check.chat_id, TEST_MESSAGE):
        return 1

    deadline = time.monotonic() + SMOKE_TEST_TIMEOUT_SECONDS
    sent = client.send_message(check.chat_id, TEST_MESSAGE)
    _log(
        f"Waiting up to {SMOKE_TEST_TIMEOUT_SECONDS}s for "
        f"outgoingMessageStatus: read ..."
    )

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining < RECEIVE_TIMEOUT_MIN:
            break
        poll_timeout = int(min(RECEIVE_TIMEOUT_DEFAULT, remaining))
        poll_timeout = max(RECEIVE_TIMEOUT_MIN, poll_timeout)

        notifications = client.receive_notification(receive_timeout=poll_timeout)
        if not notifications:
            continue

        for notification in notifications:
            if notification.receipt_id is not None:
                try:
                    client.delete_notification(notification.receipt_id)
                except ConfigError as exc:
                    _log_warn(f"Could not delete notification: {exc}")

            if (
                notification.type_webhook == "outgoingMessageStatus"
                and notification.id_message == sent.id_message
                and notification.status
            ):
                _log(f"outgoingMessageStatus: {notification.status}")
                if notification.status in TERMINAL_STATUSES:
                    if notification.status == "read":
                        _log("Smoke test PASSED: message status is 'read'.")
                        return 0
                    _log_error(
                        f"Smoke test FAILED: terminal status "
                        f"{notification.status!r}."
                    )
                    return 3

    _log_error("Smoke test FAILED: timed out waiting for 'read' status.")
    return 4


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments. Exactly one mode must be selected."""
    parser = argparse.ArgumentParser(
        description="GREEN-API MAX smoke test (never sends without confirmation)."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="Validate configuration and run CheckAccount only; never sends.",
    )
    mode.add_argument(
        "--send-and-wait-read",
        action="store_true",
        help="Send one message after confirmation and wait for the read status.",
    )
    return parser.parse_args(argv)




def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.check_only:
            return run_check_only()
        return run_send_and_wait_read()
    except ConfigError as exc:
        _log_error(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
