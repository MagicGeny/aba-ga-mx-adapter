"""GREEN-API MAX HTTP client.

Implements the three methods the first release needs:

* ``getStateInstance`` -- instance health gate before every send;
* ``checkAccount``    -- resolve a phone number to a MAX ``chatId``;
* ``sendMessage``     -- send a plain text message.

Security rules enforced here:

* the API token is part of the request URL path and is **never** placed in a
  log record or an exception message;
* exception messages carry only the operation name and the exception type /
  HTTP status, never the full URL or response body;
* the full user message text is never logged.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from ..errors import (
    CheckAccountRateLimitedError,
    DeliveryUnknownError,
    GreenApiError,
    MessageTooLongError,
    SessionExpiredError,
    SendMessageRejectedError,
    WorkerUnavailableError,
    ERROR_CODE_INSTANCE_BLOCKED,
    ERROR_CODE_INSTANCE_SUSPENDED,
    ERROR_CODE_UNKNOWN_INSTANCE_STATE,
)

logger = logging.getLogger(__name__)

# The Go orchestrator stores every campaign phone as 10 digits starting with
# '9' (internal/domain/phone.go NormalizePhone, kept by usecase/campaign.go).
# GREEN-API MAX CheckAccount, however, requires the international format:
# digits only, 11 digits with country code '7' (RU) or 12 digits with '375'
# (BY). See https://green-api.com/v3/docs/api/service/CheckAccount/
GO_CANONICAL_PHONE_REGEX = re.compile(r"^9\d{9}$")
GREEN_API_PHONE_REGEX = re.compile(r"^(?:7\d{10}|375\d{9})$")


def _digits_only(raw: str) -> str:
    return "".join(c for c in (raw or "") if c.isdigit())


def to_go_canonical_phone(raw: str) -> Optional[str]:
    """Return the Go canonical 10-digit ``9XXXXXXXXX`` form, or ``None``.

    Mirrors ``domain.NormalizePhone``: strip non-digits, drop a leading
    ``7``/``8`` country/trunk prefix when followed by ``9``.
    """
    digits = _digits_only(raw)
    if len(digits) > 10 and digits[0] in ("7", "8") and digits[1] == "9":
        digits = digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return digits
    return None


def to_green_api_phone(raw: str) -> Optional[str]:
    """Convert a phone number to GREEN-API's international wire format.

    Accepts the Go canonical form (``9XXXXXXXXX``) and already-international
    numbers (``7XXXXXXXXXX`` / ``375XXXXXXXXX``); anything else returns ``None``
    (caller reports ``INVALID_PHONE``).
    """
    digits = _digits_only(raw)
    if len(digits) == 11 and digits[0] == "7":
        return digits
    if len(digits) == 12 and digits.startswith("375"):
        return digits
    canonical = to_go_canonical_phone(digits)
    if canonical is not None:
        return "7" + canonical
    return None

# SendMessage text length limit enforced by GREEN-API MAX.
MAX_MESSAGE_LENGTH = 4000

# ReceiveNotification long-poll receiveTimeout range (seconds) and the HTTP
# timeout margin so a server-side long poll is never cut off by the client.
RECEIVE_TIMEOUT_MIN = 5
RECEIVE_TIMEOUT_MAX = 60
RECEIVE_TIMEOUT_DEFAULT = 10
RECEIVE_TIMEOUT_MARGIN_SECONDS = 15.0

# GREEN-API recommends pausing CheckAccount for 2 hours after a 469
# ("User get contact info limit reached").
CHECK_ACCOUNT_COOLDOWN_SECONDS = 2 * 60 * 60

_STATE_AUTHORIZED = "authorized"
_STATE_NOT_AUTHORIZED = "notAuthorized"
_STATE_PENDING_PASSWORD = "pendingPassword"
_STATE_STARTING = "starting"
_STATE_BLOCKED = "blocked"
_STATE_SUSPENDED = "suspended"


@dataclass(frozen=True)
class GetStateInstance:
    state: str

    @property
    def is_authorized(self) -> bool:
        return self.state == _STATE_AUTHORIZED


@dataclass(frozen=True)
class CheckAccountResult:
    exist: bool
    chat_id: str
    from_cache: Optional[bool]

    @property
    def has_valid_chat(self) -> bool:
        return self.exist and bool(self.chat_id)


@dataclass(frozen=True)
class SendMessageResult:
    id_message: str


def require_authorized(state: str) -> None:
    """Map a ``getStateInstance`` state onto the first-release send gate.

    Only ``authorized`` allows sending. Every other state raises a classified
    :class:`~aba_ga_mx_adapter.errors.AdapterError`:

    * ``notAuthorized`` / ``pendingPassword`` -> ``SESSION_EXPIRED`` (retryable)
    * ``starting``                            -> ``WORKER_UNAVAILABLE`` (retryable)
    * ``blocked``                             -> ``INSTANCE_BLOCKED`` (final)
    * ``suspended``                           -> ``INSTANCE_SUSPENDED`` (final)
    * unknown                                 -> ``UNKNOWN_INSTANCE_STATE`` (final)
    """
    if state == _STATE_AUTHORIZED:
        return
    if state in (_STATE_NOT_AUTHORIZED, _STATE_PENDING_PASSWORD):
        raise SessionExpiredError(
            f"Instance is not authorized (stateInstance={state})"
        )
    if state == _STATE_STARTING:
        raise WorkerUnavailableError(
            f"Instance is starting (stateInstance={state})"
        )
    if state == _STATE_BLOCKED:
        raise GreenApiError(
            f"Instance is blocked (stateInstance={state})",
            error_code=ERROR_CODE_INSTANCE_BLOCKED,
        )
    if state == _STATE_SUSPENDED:
        raise GreenApiError(
            f"Instance is suspended (stateInstance={state})",
            error_code=ERROR_CODE_INSTANCE_SUSPENDED,
        )
    raise GreenApiError(
        f"Unknown instance state (stateInstance={state})",
        error_code=ERROR_CODE_UNKNOWN_INSTANCE_STATE,
    )


class GreenApiMaxClient:
    """Async GREEN-API client for a single MAX instance."""

    def __init__(
        self,
        *,
        base_url: str,
        id_instance: str,
        token_instance: str,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._id_instance = id_instance
        self._token_instance = token_instance
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._check_account_cooldown_until: Optional[float] = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Content-Type": "application/json"},
        )
        # httpx logs the full request URL at INFO, and the URL embeds the API
        # token. Keep those library loggers quiet so the token can never leak
        # into the adapter's logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    # -- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GreenApiMaxClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    # -- public API ----------------------------------------------------------

    def check_account_rate_limited(self) -> bool:
        """True while the 2-hour CheckAccount cooldown after a 469 is active."""
        if self._check_account_cooldown_until is None:
            return False
        if self._clock() >= self._check_account_cooldown_until:
            self._check_account_cooldown_until = None
            return False
        return True

    async def get_state_instance(self) -> GetStateInstance:
        url = self._method_url("getStateInstance")
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"GetStateInstance request failed ({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"GetStateInstance authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"GetStateInstance temporarily unavailable ({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"GetStateInstance API error ({self._safe_detail(resp)})"
            )

        data = self._parse_json_object(resp, "GetStateInstance")
        state = data.get("stateInstance")
        if not isinstance(state, str) or not state:
            raise GreenApiError(
                "GetStateInstance response missing stateInstance",
                error_code=ERROR_CODE_UNKNOWN_INSTANCE_STATE,
            )
        # Gate here so callers can never send unless the instance is
        # authorized; classified errors carry a secret-safe state string only.
        require_authorized(state)
        return GetStateInstance(state=state)

    async def check_account(self, phone: str) -> CheckAccountResult:
        """Resolve a phone number to a MAX chat.

        ``phone`` must already be validated with :data:`PHONE_REGEX`.
        """
        if self.check_account_rate_limited():
            raise CheckAccountRateLimitedError(
                "CheckAccount is in cooldown after a GREEN-API 469 rate limit"
            )

        url = self._method_url("checkAccount")
        # GREEN-API expects a JSON *integer* phone number and no `force` flag.
        body = {"phoneNumber": int(phone)}
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"CheckAccount request failed ({type(exc).__name__})"
            ) from None

        if self._is_rate_limited_response(resp):
            self._check_account_cooldown_until = (
                self._clock() + CHECK_ACCOUNT_COOLDOWN_SECONDS
            )
            logger.warning(
                "GREEN_API_CHECK_ACCOUNT_LIMIT status=%s: pausing CheckAccount "
                "for %s seconds",
                resp.status_code,
                CHECK_ACCOUNT_COOLDOWN_SECONDS,
            )
            raise CheckAccountRateLimitedError(
                "GREEN-API user contact info limit reached (469); "
                "CheckAccount is paused for 2 hours"
            )

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"CheckAccount authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"CheckAccount temporarily unavailable ({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"CheckAccount API error ({self._safe_detail(resp)})"
            )

        data = self._parse_json_object(resp, "CheckAccount")

        # `status=false` is an API/instance error, never proof the recipient
        # has no MAX account.
        if data.get("status") is False:
            raise GreenApiError(
                f"CheckAccount returned status=false ({self._safe_detail(resp)})"
            )

        if "exist" not in data:
            raise GreenApiError(
                "CheckAccount response missing 'exist' field"
            )

        exist = data.get("exist")
        if not isinstance(exist, bool):
            raise GreenApiError(
                "CheckAccount response 'exist' is not a boolean"
            )

        if not exist:
            return CheckAccountResult(exist=False, chat_id="", from_cache=None)

        chat_id = data.get("chatId")
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            chat_id = str(chat_id)
        if not isinstance(chat_id, str) or not chat_id.strip():
            # exist=true without a usable chatId is an API error, not a
            # "user not found": never report it as exist=false.
            raise GreenApiError(
                "CheckAccount returned exist=true without a usable chatId"
            )

        from_cache = data.get("fromCache")
        return CheckAccountResult(
            exist=True,
            chat_id=chat_id,
            from_cache=from_cache if isinstance(from_cache, bool) else None,
        )

    async def send_message(self, chat_id: str, message: str) -> SendMessageResult:
        if len(message) > MAX_MESSAGE_LENGTH:
            raise MessageTooLongError(
                f"message_text exceeds {MAX_MESSAGE_LENGTH} characters "
                f"(got {len(message)})"
            )

        url = self._method_url("sendMessage")
        body = {"chatId": chat_id, "message": message}
        try:
            resp = await self._client.post(url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached the server: safe to retry.
            raise WorkerUnavailableError(
                f"SendMessage not sent: connection failed ({type(exc).__name__})"
            ) from None
        except httpx.HTTPError as exc:
            # The request may or may not have reached the server: the outcome
            # is unknown and the message must not be retried automatically.
            raise DeliveryUnknownError(
                f"SendMessage outcome unknown after transport error "
                f"({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"SendMessage authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            # An explicit error response means GREEN-API did not accept the
            # message. Final diagnostic code: Go will not auto-retry.
            raise SendMessageRejectedError(
                f"SendMessage rejected by GREEN-API ({self._safe_detail(resp)})"
            )

        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise DeliveryUnknownError(
                "SendMessage response was not valid JSON: delivery status unknown"
            )

        id_message = data.get("idMessage")
        if not isinstance(id_message, str) or not id_message:
            raise DeliveryUnknownError(
                "SendMessage response missing idMessage: delivery status unknown"
            )
        return SendMessageResult(id_message=id_message)

    # -- ReceiveNotification / DeleteNotification ---------------------------

    async def receive_notification(
        self, receive_timeout: int = RECEIVE_TIMEOUT_DEFAULT
    ) -> Optional[list[dict]]:
        """Long-poll ReceiveNotification.

        Returns the list of notifications (possibly empty). A timeout just means
        no notification arrived within the window. Transport/API failures raise
        a classified :class:`~aba_ga_mx_adapter.errors.AdapterError`.
        """
        if not (RECEIVE_TIMEOUT_MIN <= receive_timeout <= RECEIVE_TIMEOUT_MAX):
            receive_timeout = RECEIVE_TIMEOUT_DEFAULT
        request_timeout = float(receive_timeout) + RECEIVE_TIMEOUT_MARGIN_SECONDS

        url = self._method_url("receiveNotification")
        params = {"receiveTimeout": receive_timeout}
        try:
            resp = await self._client.get(url, params=params, timeout=request_timeout)
        except httpx.TimeoutException:
            # No notification within the window.
            return []
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"ReceiveNotification request failed ({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"ReceiveNotification authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"ReceiveNotification temporarily unavailable "
                f"({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"ReceiveNotification API error ({self._safe_detail(resp)})"
            )

        try:
            data = resp.json()
        except ValueError:
            return []
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    async def delete_notification(self, receipt_id: int) -> bool:
        """Delete a processed notification from the instance queue."""
        url = f"{self._method_url('deleteNotification')}/{receipt_id}"
        try:
            resp = await self._client.delete(url)
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"DeleteNotification request failed ({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"DeleteNotification authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"DeleteNotification temporarily unavailable "
                f"({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"DeleteNotification API error ({self._safe_detail(resp)})"
            )
        return True

    async def get_settings(self) -> dict:
        """Fetch the instance settings (webhook flags, send delay, etc.)."""
        url = self._method_url("getSettings")
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"GetSettings request failed ({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"GetSettings authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"GetSettings temporarily unavailable ({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"GetSettings API error ({self._safe_detail(resp)})"
            )
        return self._parse_json_object(resp, "GetSettings")

    async def set_settings(self, settings: dict) -> bool:
        """Apply a partial settings update (SetSettings).

        WARNING: GREEN-API restarts the instance on this call; settings take
        up to ~5 minutes to apply. Never call this from the runtime worker —
        it exists for the explicit one-off `--enable-incoming` command.
        """
        url = self._method_url("setSettings")
        try:
            resp = await self._client.post(url, json=settings)
        except httpx.HTTPError as exc:
            raise WorkerUnavailableError(
                f"SetSettings request failed ({type(exc).__name__})"
            ) from None

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"SetSettings authorization failed (HTTP {resp.status_code})"
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise WorkerUnavailableError(
                f"SetSettings temporarily unavailable ({self._safe_detail(resp)})"
            )
        if resp.status_code != 200:
            raise GreenApiError(
                f"SetSettings API error ({self._safe_detail(resp)})"
            )
        data = self._parse_json_object(resp, "SetSettings")
        return data.get("saveSettings") is True

    # -- internals -----------------------------------------------------------

    def _method_url(self, method: str) -> str:
        """Build a GREEN-API method URL.

        The returned URL embeds the API token and must never be logged or
        included in an exception message.
        """
        return (
            f"{self._base_url}/waInstance{self._id_instance}/{method}/{self._token_instance}"
        )

    def _parse_json_object(self, resp: httpx.Response, operation: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise WorkerUnavailableError(
                f"{operation} returned an unparsable response"
            ) from None
        if not isinstance(data, dict):
            raise WorkerUnavailableError(
                f"{operation} returned a non-object response"
            )
        return data

    @staticmethod
    def _is_rate_limited_response(resp: httpx.Response) -> bool:
        if resp.status_code == 469:
            return True
        try:
            data = resp.json()
        except ValueError:
            return False
        if isinstance(data, dict):
            code = data.get("code")
            return code == 469 or code == "469"
        return False

    @staticmethod
    def _safe_detail(resp: httpx.Response) -> str:
        """Short, secret-safe diagnostic for an error response.

        Never includes the URL, the token, or the full body.
        """
        parts = [f"HTTP {resp.status_code}"]
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            code = data.get("code", data.get("error_code"))
            message = data.get("message", data.get("error"))
            if code not in (None, ""):
                parts.append(f"code={code}")
            if message:
                parts.append(f"message={str(message)[:200]}")
        return ", ".join(parts)
