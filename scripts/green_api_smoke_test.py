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

This script NEVER sends messages automatically.  You must explicitly pass a
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
RECEIVE_TIMEOUT_MIN = 5
RECEIVE_TIMEOUT_MAX = 60
RECEIVE_TIMEOUT_DEFAULT = 10

# Phone number validation: digits only, 7-15 digits (GREEN-API requirement:
# "must contain only digits").  GREEN-API MAX specifically restricts to
# 11-12 digits with RF ("7") / RB ("375") prefixes; the server enforces that
# and returns a 400 on mismatch.
PHONE_REGEX = re.compile(r"^\d{7,15}$")

# GREEN-API OutgoingMessageStatus terminal status values.
TERMINAL_STATUSES = {"read", "failed", "noAccount", "notInGroup"}

# The notification type for outgoing message status.
STATUS_NOTIFICATION_TYPE = "outgoingMessageStatus"


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
# URL helpers
# ---------------------------------------------------------------------------

def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes from the base URL."""
    return base_url.rstrip("/")


def _sanitize_url(url: str) -> str:
    """Return a URL with the API token masked for logging.

    GREEN-API places the API token in the URL path.  We must never print it.
    """
    parts = url.split("/")
    if len(parts) >= 2 and parts[-1]:
        parts[-1] = "***"
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CheckAccountResult:
    """Parsed response from the GREEN-API CheckAccount method."""

    exist: bool
    chat_id: Optional[str]
    from_cache: Optional[bool]
    raw_response: dict[str, Any]

    @property
    def has_valid_chat(self) -> bool:
        """True when exist=true and a non-empty chatId was returned."""
        return self.exist and bool(self.chat_id)


@dataclass
class SendMessageResult:
    """Parsed response from the GREEN-API SendMessage method."""

    id_message: str
    raw_response: dict[str, Any]


@dataclass
class Notification:
    """A single notification extracted from a ReceiveNotification response.

    The ReceiveNotification API wraps the notification in a *body* field:

        {"receiptId": 1234567, "body": {"typeWebhook": "outgoingMessageStatus", ...}}
    """

    receipt_id: Optional[int]
    type_webhook: Optional[str]
    id_message: Optional[str]
    status: Optional[str]
    chat_id: Optional[str]
    description: Optional[str]
    timestamp: Optional[int]
    raw_body: dict[str, Any]

    @classmethod
    def from_raw(cls, item: dict[str, Any]) -> "Notification":
        """Parse one ReceiveNotification envelope into a Notification."""
        body = item.get("body")
        if not isinstance(body, dict):
            body = {}
        return cls(
            receipt_id=item.get("receiptId"),
            type_webhook=body.get("typeWebhook"),
            id_message=body.get("idMessage"),
            status=body.get("status"),
            chat_id=body.get("chatId"),
            description=body.get("description"),
            timestamp=body.get("timestamp"),
            raw_body=dict(body),
        )

    @property
    def is_status_notification(self) -> bool:
        """True for an outgoing-message-status notification."""
        return self.type_webhook == STATUS_NOTIFICATION_TYPE

    def is_for_message(self, id_message: str) -> bool:
        """True when this is an outgoingMessageStatus for *id_message*."""
        return (
            self.is_status_notification
            and self.id_message is not None
            and self.id_message == id_message
        )

    def is_unrelated(self, id_message: str) -> bool:
        """True when this notification does not belong to *id_message*."""
        return not self.is_for_message(id_message)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def validate_config() -> None:
    """Validate that all required environment variables are set correctly.

    Raises ConfigError with a safe diagnostic (no credentials).
    """
    missing: list[str] = []
    if not GREEN_API_BASE_URL:
        missing.append("GREEN_API_BASE_URL")
    if not GREEN_API_ID_INSTANCE:
        missing.append("GREEN_API_ID_INSTANCE")
    if not GREEN_API_TOKEN_INSTANCE:
        missing.append("GREEN_API_TOKEN_INSTANCE")
    if not TEST_PHONE_NUMBER:
        missing.append("TEST_PHONE_NUMBER")
    if not TEST_MESSAGE:
        missing.append("TEST_MESSAGE")
    if missing:
        raise ConfigError(
            "Missing required configuration: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )

    if not PHONE_REGEX.match(TEST_PHONE_NUMBER):
        raise ConfigError(
            "TEST_PHONE_NUMBER is invalid. Expected 7-15 digits only "
            "(GREEN-API requires digits, no '+', no spaces), got: "
            f"{TEST_PHONE_NUMBER!r}"
        )

    if not GREEN_API_BASE_URL.startswith("https://"):
        raise ConfigError("GREEN_API_BASE_URL must start with 'https://'.")


# ---------------------------------------------------------------------------
# JSON / error helpers
# ---------------------------------------------------------------------------

def _safe_parse_json(resp: httpx.Response, context: str) -> Any:
    """Safely parse a JSON response body."""
    try:
        return resp.json()
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"{context}: failed to parse JSON response ({exc}). "
            f"HTTP {resp.status_code}."
        ) from exc


def _raise_api_error(method: str, resp: httpx.Response) -> None:
    """Raise a ConfigError with a safe diagnostic from an API error response.

    GREEN-API standard error responses contain ``error_code`` and
    ``error_message`` fields, or sometimes ``status`` / ``reason``.
    No credentials are included in the message.
    """
    status_code = resp.status_code
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            if data.get("error_message"):
                detail = f" - {data['error_message']}"
            elif data.get("message"):
                detail = f" - {data['message']}"
            elif data.get("reason"):
                detail = f" - {data['reason']}"
            elif data.get("error_code") is not None:
                detail = f" - error_code={data['error_code']}"
    except (ValueError, TypeError):
        pass
    raise ConfigError(f"{method} API error: HTTP {status_code}{detail}")


# ---------------------------------------------------------------------------
# GREEN-API client
# ---------------------------------------------------------------------------

class GreenApiClient:
    """Thin HTTP client for the GREEN-API MAX HTTP API."""

    def __init__(
        self,
        base_url: str,
        id_instance: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._id_instance = id_instance
        self._token = token
        self._timeout = timeout
        self._client: Optional[httpx.Client] = None

    # -- internal helpers --------------------------------------------------

    def _get_client(self) -> httpx.Client:
        """Return a lazily-created httpx client."""
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _wa_url(self, method_name: str) -> str:
        """Build a GREEN-API v3 waInstance URL (token is never printed)."""
        return (
            f"{self._base_url}/waInstance"
            f"{self._id_instance}/{method_name}/{self._token}"
        )

    # -- CheckAccount -----------------------------------------------------

    def check_account(
        self, phone_number: str, force: bool = False
    ) -> CheckAccountResult:
        """Call GREEN-API CheckAccount.

        See: https://green-api.com/v3/docs/api/service/CheckAccount/

        ``force`` defaults to ``False`` per the contract (use the 2-hour cache).
        ``phoneNumber`` is sent as an integer per the documented type.
        """
        body: dict[str, Any] = {"phoneNumber": int(phone_number)}
        if force:
            body["force"] = True

        _log("CheckAccount -> sending request (URL hidden)")

        try:
            resp = self._get_client().post(
                self._wa_url("checkAccount"), json=body
            )
        except httpx.TimeoutException as exc:
            raise ConfigError(f"CheckAccount request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise ConfigError(f"CheckAccount connection failed: {exc}") from exc

        if resp.status_code != 200:
            _raise_api_error("CheckAccount", resp)

        data = _safe_parse_json(resp, "CheckAccount")

        if not isinstance(data, dict):
            raise ConfigError(
                "CheckAccount returned a non-object response: "
                f"{type(data).__name__}"
            )

        # GREEN-API returns {"status": false, "reason": "..."} for
        # instance-not-ready or rate-limit conditions.  These are NOT
        # proof that the recipient doesn't exist.
        if "exist" not in data:
            if data.get("status") is False:
                reason = data.get("reason", "unknown")
                raise ConfigError(
                    f"CheckAccount reports an instance-level error: {reason}. "
                    "This is NOT proof that the recipient is absent."
                )
            raise ConfigError(
                f"CheckAccount response missing 'exist' field: {data}"
            )

        exist = data.get("exist")
        chat_id = data.get("chatId")
        from_cache = data.get("fromCache")

        if not exist:
            chat_id = None
            from_cache = None

        result = CheckAccountResult(
            exist=bool(exist),
            chat_id=chat_id if isinstance(chat_id, str) else None,
            from_cache=bool(from_cache) if from_cache is not None else None,
            raw_response=dict(data),
        )

        if result.exist and result.from_cache is not None:
            _log(
                f"CheckAccount OK: exist={result.exist}, "
                f"chatId={result.chat_id}, fromCache={result.from_cache}"
            )
        elif result.exist:
            _log(
                f"CheckAccount OK: exist={result.exist}, "
                f"chatId={result.chat_id}"
            )

        return result

    # -- SendMessage ------------------------------------------------------

    def send_message(self, chat_id: str, message: str) -> SendMessageResult:
        """Call GREEN-API SendMessage.

        See: https://green-api.com/v3/docs/api/sending/SendMessage/

        Sends JSON containing ``chatId`` and ``message``.
        """
        body: dict[str, Any] = {"chatId": chat_id, "message": message}
        _log("SendMessage -> sending request (URL hidden)")

        try:
            resp = self._get_client().post(
                self._wa_url("sendMessage"), json=body
            )
        except httpx.TimeoutException as exc:
            raise ConfigError(
                f"SendMessage request timed out: {exc}. The message MAY "
                "have been sent. Check the recipient's device before retrying."
            ) from exc
        except httpx.ConnectError as exc:
            raise ConfigError(f"SendMessage connection failed: {exc}") from exc

        if resp.status_code != 200:
            _raise_api_error("SendMessage", resp)

        data = _safe_parse_json(resp, "SendMessage")

        if not isinstance(data, dict):
            raise ConfigError(
                f"SendMessage returned a non-object response: "
                f"{type(data).__name__}"
            )

        id_message = data.get("idMessage")
        if not id_message or not isinstance(id_message, str):
            raise ConfigError(
                f"SendMessage response missing 'idMessage': {data}"
            )

        _log(f"SendMessage accepted. idMessage={id_message}")
        return SendMessageResult(
            id_message=id_message, raw_response=dict(data)
        )

    # -- ReceiveNotification ----------------------------------------------

    def receive_notification(
        self, receive_timeout: int = RECEIVE_TIMEOUT_DEFAULT
    ) -> Optional[list[dict[str, Any]]]:
        """Call GREEN-API ReceiveNotification.

        See: https://green-api.com/v3/docs/api/receiving/technology-http-api/ReceiveNotification/

        ``receive_timeout``: 5-60 seconds (GREEN-API documented range).
        Returns a list of notification envelopes, or ``None`` if no
        notification was available within the timeout.
        """
        if not (RECEIVE_TIMEOUT_MIN <= receive_timeout <= RECEIVE_TIMEOUT_MAX):
            receive_timeout = RECEIVE_TIMEOUT_DEFAULT

        try:
            resp = self._get_client().get(
                self._wa_url("receiveNotification"),
                params={"receiveTimeout": receive_timeout},
            )
        except httpx.TimeoutException:
            # A timeout here just means no notification was available.
            return None
        except httpx.ConnectError as exc:
            raise ConfigError(
                f"ReceiveNotification connection failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            _raise_api_error("ReceiveNotification", resp)

        data = _safe_parse_json(resp, "ReceiveNotification")

        if data is None:
            return None

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # An empty dict means "no notification" (timeout).
            if not data:
                return None
            return [data]
        return None

    # -- DeleteNotification -----------------------------------------------

    def delete_notification(self, receipt_id: int) -> tuple[bool, str]:
        """Call GREEN-API DeleteNotification.

        See: https://green-api.com/v3/docs/api/receiving/technology-http-api/DeleteNotification/

        DELETE {base}/waInstance{id}/deleteNotification/{token}/{receiptId}

        Returns ``(success, reason)`` where *reason* is an empty string on
        success.
        """
        url = f"{self._wa_url('deleteNotification')}/{receipt_id}"
        _log(
            f"DeleteNotification -> sending request "
            f"(receiptId={receipt_id}, URL hidden)"
        )

        try:
            resp = self._get_client().delete(url)
        except httpx.TimeoutException as exc:
            raise ConfigError(
                f"DeleteNotification request timed out: {exc}"
            ) from exc
        except httpx.ConnectError as exc:
            raise ConfigError(
                f"DeleteNotification connection failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            _raise_api_error("DeleteNotification", resp)

        data = _safe_parse_json(resp, "DeleteNotification")

        if not isinstance(data, dict):
            raise ConfigError(
                f"DeleteNotification returned a non-object response: "
                f"{type(data).__name__}"
            )

        success = bool(data.get("result", False))
        reason = data.get("reason", "") or ""
        if not isinstance(reason, str):
            reason = str(reason)

        return success, reason

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# Operator confirmation
# ---------------------------------------------------------------------------

def confirm_send(phone: str, message: str) -> None:
    """Display the recipient and message, then require the operator to type SEND."""
    _log("=" * 60)
    _log("READY TO SEND A TEST MESSAGE")
    _log(f"  Recipient phone: {phone}")
    _log(f"  Message text:    {message}")
    _log("=" * 60)
    _log("Type SEND to confirm and proceed (anything else cancels):")
    try:
        response = input("> ").strip()
    except EOFError:
        response = ""
    if response != "SEND":
        _log("Aborted by operator. No message was sent.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Orchestration: --check-only
# ---------------------------------------------------------------------------

def run_check_only(client: GreenApiClient, phone: str) -> int:
    """Run only the CheckAccount step."""
    _log(f"Running CheckAccount for phone: {phone}")
    try:
        result = client.check_account(phone)
    except ConfigError as exc:
        _log_error(str(exc))
        return 1

    if not result.exist:
        _log(
            f"CheckAccount: recipient does NOT have a MAX account "
            f"(exist={result.exist}, chatId='{result.chat_id}'). "
            "Stopping without sending."
        )
        return 0

    if not result.has_valid_chat:
        _log_error(
            "CheckAccount returned exist=true but no valid chatId. "
            "Stopping without sending."
        )
        return 1

    _log(
        f"CheckAccount: recipient has a MAX account. "
        f"chatId={result.chat_id}, fromCache={result.from_cache}"
    )
    _log("Check successful. No message was sent (--check-only mode).")
    return 0


# ---------------------------------------------------------------------------
# Orchestration: --send-and-wait-read
# ---------------------------------------------------------------------------

def run_send_and_wait_read(
    client: GreenApiClient,
    phone: str,
    message: str,
    timeout_seconds: int,
) -> int:
    """Run the full smoke-test sequence.

    CheckAccount -> SendMessage -> ReceiveNotification -> wait for 'read'
    """
    # --- 1. CheckAccount ------------------------------------------------
    _log(f"Step 1: CheckAccount for phone: {phone}")
    try:
        result = client.check_account(phone)
    except ConfigError as exc:
        _log_error(f"CheckAccount failed: {exc}")
        return 1

    if not result.exist:
        _log(
            f"CheckAccount: recipient does NOT have a MAX account "
            f"(exist={result.exist}). Stopping without sending."
        )
        return 1

    if not result.has_valid_chat or not result.chat_id:
        _log_error(
            "CheckAccount returned exist=true but no valid chatId. "
            "Stopping without sending."
        )
        return 1

    _log(
        f"Step 1 complete: exist={result.exist}, "
        f"chatId={result.chat_id}, fromCache={result.from_cache}"
    )

    # --- 2. Confirm with operator --------------------------------------
    confirm_send(phone, message)

    # --- 3. SendMessage --------------------------------------------------
    _log("Step 2: SendMessage")
    try:
        send_result = client.send_message(result.chat_id, message)
    except ConfigError as exc:
        _log_error(f"SendMessage failed: {exc}")
        return 1

    id_message = send_result.id_message
    _log(f"Step 2 complete: SendMessage accepted. idMessage={id_message}")
    _log(
        "NOTE: SendMessage success means the request was ACCEPTED, "
        "not that the message was delivered or read."
    )

    # --- 4. ReceiveNotification -> wait for read -----------------------
    _log(
        f"Step 3: Polling ReceiveNotification for 'read' status "
        f"(idMessage={id_message}, timeout={timeout_seconds}s)"
    )
    _log("  Open the delivered test message on the recipient's device.")

    start_time = time.monotonic()
    last_intermediate: Optional[str] = None

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed

        if remaining <= 0:
            _log_error(
                f"Timeout ({timeout_seconds}s) reached. No 'read' status "
                f"received for idMessage={id_message}."
            )
            if last_intermediate:
                _log(f"  Last observed status: {last_intermediate}")
            _log(
                "  The message may still be waiting for the recipient to open it."
            )
            return 1

        receive_timeout = max(
            RECEIVE_TIMEOUT_MIN,
            min(RECEIVE_TIMEOUT_MAX, int(remaining)),
        )

        try:
            items = client.receive_notification(receive_timeout)
        except ConfigError as exc:
            _log_error(f"ReceiveNotification failed: {exc}")
            return 1

        if items is None:
            elapsed = time.monotonic() - start_time
            _log(
                f"  No notification yet (elapsed {elapsed:.0f}s/"
                f"{timeout_seconds}s). Continuing to poll..."
            )
            continue

        for item in items:
            notif = Notification.from_raw(item)

            # --- Unrelated notification: report and stop, do NOT delete ---
            if notif.is_unrelated(id_message):
                _log_warn(
                    f"Queue contains an UNRELATED notification: "
                    f"typeWebhook={notif.type_webhook!r}, "
                    f"idMessage={notif.id_message!r}, "
                    f"status={notif.status!r}"
                )
                _log_warn(
                    "This notification does not belong to the test message. "
                    "It will NOT be deleted. Stopping."
                )
                _log_warn(
                    "For a clean run, use a dedicated test instance "
                    "with an empty notification queue."
                )
                return 1

            status = notif.status
            _log(
                f"  Received outgoingMessageStatus: status={status} "
                f"idMessage={notif.id_message}"
            )

            # Delete the handled notification from the queue.
            if notif.receipt_id is not None:
                try:
                    deleted, reason = client.delete_notification(notif.receipt_id)
                    if deleted:
                        _log(f"  Deleted notification (receiptId={notif.receipt_id})")
                    else:
                        _log_warn(
                            f"  DeleteNotification result=false for "
                            f"receiptId={notif.receipt_id}: {reason or 'unknown'}"
                        )
                except ConfigError as exc:
                    _log_warn(f"  DeleteNotification failed: {exc}")
            else:
                _log_warn("  Notification has no receiptId; cannot delete.")

            # --- Evaluate status ---
            if status == "read":
                _log(
                    f"SUCCESS: recipient READ the message "
                    f"(idMessage={id_message})."
                )
                return 0

            if status in TERMINAL_STATUSES:
                _log_error(
                    f"Terminal error status '{status}' for idMessage={id_message}."
                )
                if notif.description:
                    _log(f"  Description: {notif.description}")
                return 1

            # Intermediate status (delivered, or any non-terminal value).
            last_intermediate = status
            _log(f"  Intermediate status '{status}'. Waiting for 'read'...")

    return 1  # Unreachable — the loop always returns or times out.


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="green_api_smoke_test",
        description="GREEN-API MAX smoke test: "
        "CheckAccount -> SendMessage -> ReceiveNotification -> read",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check-only",
        action="store_true",
        help="Call CheckAccount only. Do NOT send a message or poll the "
        "notification queue.",
    )
    group.add_argument(
        "--send-and-wait-read",
        action="store_true",
        help="Run the full smoke test: CheckAccount -> SendMessage -> "
        "ReceiveNotification -> wait for 'read' status.",
    )
    args = parser.parse_args(argv)

    try:
        validate_config()
    except ConfigError as exc:
        _log_error(f"Configuration error: {exc}")
        return 1

    client = GreenApiClient(
        base_url=GREEN_API_BASE_URL,
        id_instance=GREEN_API_ID_INSTANCE,
        token=GREEN_API_TOKEN_INSTANCE,
    )

    try:
        if args.check_only:
            return run_check_only(client, TEST_PHONE_NUMBER)
        elif args.send_and_wait_read:
            return run_send_and_wait_read(
                client,
                phone=TEST_PHONE_NUMBER,
                message=TEST_MESSAGE,
                timeout_seconds=SMOKE_TEST_TIMEOUT_SECONDS,
            )
    finally:
        client.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())


