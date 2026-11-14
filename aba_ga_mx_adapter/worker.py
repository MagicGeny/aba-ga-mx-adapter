"""Task processing and the RabbitMQ worker loop.

Two separable pieces:

* :class:`TaskProcessor` -- the whole business logic for one task. It depends
  only on a GREEN-API client, so it is testable without RabbitMQ and without a
  real API.
* :class:`AdapterWorker` -- RabbitMQ consumption with the required ack
  ordering (result published and confirmed *before* the original delivery is
  acked) and dead-lettering of structurally broken tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Optional

from .admin_notify import (
    format_notification_message,
    parse_admin_notification_task,
)
from .errors import (
    AccountMismatchError,
    AdapterError,
    CheckAccountRateLimitedError,
    ERROR_CODE_DELIVERY_UNKNOWN,
    ERROR_CODE_GREEN_API_ERROR,
    ERROR_CODE_INTERNAL_ERROR,
    GreenApiError,
    InvalidPhoneError,
    InvalidTaskError,
    InvalidMessageTextError,
    MESSENGER_TYPE_MAX,
    MissingChatIdError,
    SendMessageRejectedError,
    STATUS_DELIVERY_UNKNOWN,
    STATUS_FAILED,
    UnsupportedAttachmentError,
    UnsupportedMessengerTypeError,
    UserNotFoundByPhoneError,
)
from .green_api import (
    GreenApiMaxClient,
    require_authorized,
    to_go_canonical_phone,
    to_green_api_phone,
)
from .incoming import notification_summary, notification_to_results
from .messaging import GatewayStoppedError
from .models import MAX_MESSAGE_LENGTH, SendTaskPayload, TargetResult

logger = logging.getLogger(__name__)


def mask_phone(phone: str) -> str:
    """Mask the middle of a phone number for logs (never log a full MSISDN)."""
    if not phone:
        return ""
    if len(phone) <= 4:
        return "***"
    return f"{phone[:3]}***{phone[-2:]}"


class TaskProcessor:
    """Process exactly one :class:`SendTaskPayload` into a :class:`TargetResult`."""

    def __init__(
        self,
        *,
        tenant_account_id: str,
        client: GreenApiMaxClient,
    ) -> None:
        self._tenant_account_id = tenant_account_id
        self._client = client

    async def process_task(self, task: SendTaskPayload) -> TargetResult:
        """Always returns a result (except on cancellation).

        Any error becomes a ``TargetResult`` with a status and error code the
        Go orchestrator understands. The message is never sent twice.
        """
        chat_id_used: Optional[str] = None
        send_dispatched = False
        try:
            self._validate(task)

            state = await self._client.get_state_instance()
            require_authorized(state.state)

            if task.use_chat_id:
                chat_id_used = task.chat_id
            else:
                chat_id_used = await self._resolve_chat_id(task)

            send_dispatched = True
            send_result = await self._client.send_message(
                chat_id_used, task.message_text
            )

            logger.info(
                "TASK_SENT task_id=%s campaign_id=%s chat_id=%s phone=%s idMessage=%s",
                task.task_id,
                task.campaign_id,
                chat_id_used,
                mask_phone(task.phone),
                send_result.id_message,
            )
            return TargetResult.sent(task, chat_id=chat_id_used)
        except AdapterError as exc:
            return self._error_result(task, exc, chat_id=chat_id_used or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - must never crash the worker
            # An unexpected error: only send_message can have sent anything, and
            # it classifies its own failures. If the flag is set the request was
            # already dispatched, so the safe outcome is "unknown", otherwise
            # the task failed before anything was sent.
            logger.error(
                "TASK_INTERNAL_ERROR task_id=%s error=%s",
                task.task_id,
                type(exc).__name__,
            )
            if send_dispatched:
                return TargetResult.for_task(
                    task,
                    status=STATUS_DELIVERY_UNKNOWN,
                    error_code=ERROR_CODE_DELIVERY_UNKNOWN,
                    error_message="unexpected error after SendMessage dispatch",
                    chat_id=chat_id_used or "",
                )
            return TargetResult.for_task(
                task,
                status=STATUS_FAILED,
                error_code=ERROR_CODE_INTERNAL_ERROR,
                error_message="internal adapter error",
            )

    def _error_result(
        self, task: SendTaskPayload, exc: AdapterError, *, chat_id: str
    ) -> TargetResult:
        if exc.task_status != STATUS_DELIVERY_UNKNOWN:
            chat_id = ""
        logger.info(
            "TASK_FAILED task_id=%s phone=%s status=%s error_code=%s detail=%s",
            task.task_id,
            mask_phone(task.phone),
            exc.task_status,
            exc.error_code,
            exc.message,
        )
        return TargetResult.for_task(
            task,
            status=exc.task_status,
            error_code=exc.error_code,
            error_message=exc.message,
            chat_id=chat_id,
        )

    # -- validation ----------------------------------------------------------

    def _validate(self, task: SendTaskPayload) -> None:
        """Pre-API validation. No GREEN-API call happens before this passes."""
        if task.tenant_account_id != self._tenant_account_id:
            raise AccountMismatchError(
                "task tenant_account_id does not match this adapter process"
            )
        if task.account_id != task.tenant_account_id:
            raise AccountMismatchError(
                "task account_id does not match tenant_account_id"
            )
        if (task.messenger_type or "").upper() != MESSENGER_TYPE_MAX:
            raise UnsupportedMessengerTypeError(
                f"unsupported messenger_type (expected {MESSENGER_TYPE_MAX})"
            )
        if task.has_attachment():
            raise UnsupportedAttachmentError(
                "attachments are not supported in this release"
            )
        if not task.message_text:
            raise InvalidMessageTextError("message_text is empty")
        if len(task.message_text) > MAX_MESSAGE_LENGTH:
            raise InvalidMessageTextError(
                f"message_text exceeds {MAX_MESSAGE_LENGTH} characters"
            )
        if task.use_chat_id and not task.chat_id.strip():
            raise MissingChatIdError(
                "use_chat_id=true but chat_id is empty"
            )

    # -- chat resolution -----------------------------------------------------

    async def _resolve_chat_id(self, task: SendTaskPayload) -> str:
        if self._client.check_account_rate_limited():
            raise CheckAccountRateLimitedError(
                "CheckAccount is in cooldown after a GREEN-API 469 rate limit"
            )
        # The Go orchestrator sends 10 digits starting with 9; GREEN-API needs
        # the international form (7XXXXXXXXXX / 375XXXXXXXXX).
        green_api_phone = to_green_api_phone(task.phone)
        if green_api_phone is None:
            raise InvalidPhoneError(
                "phone is not a valid MAX number "
                "(expected 10 digits starting with 9, or 11 digits starting "
                "with 7, or 12 digits starting with 375)"
            )

        result = await self._client.check_account(green_api_phone)
        if not result.exist:
            raise UserNotFoundByPhoneError()
        if not result.chat_id:
            raise GreenApiError(
                "CheckAccount exist=true without a usable chatId",
                error_code=ERROR_CODE_GREEN_API_ERROR,
            )
        return result.chat_id


class AdminNotifyProcessor:
    """Send a tenant-admin notification and report the admin chat_id back."""

    def __init__(self, *, tenant_account_id: str, client: GreenApiMaxClient) -> None:
        self._tenant_account_id = tenant_account_id
        self._client = client

    async def process(self, body: dict) -> Optional[TargetResult]:
        """Return a ``sent`` result to publish, or ``None`` when nothing to do.

        Never raises for expected failures: a failed notification is logged and
        the delivery is acked, matching the Playwright adapter's behaviour.
        """
        task = parse_admin_notification_task(body)

        # Account isolation: only the account that observed the replies may send.
        if (task.tenant_account_id or "") != self._tenant_account_id:
            logger.error(
                "ADMIN_NOTIFY_ACCOUNT_MISMATCH task_account=%s worker_account=%s",
                task.tenant_account_id,
                self._tenant_account_id,
            )
            return None

        green_phone = to_green_api_phone(task.tenant_phone)
        if green_phone is None:
            logger.error("ADMIN_NOTIFY_INVALID_PHONE tenant_phone=%s", task.tenant_phone)
            return None

        text = format_notification_message(task)
        used_saved_chat = bool(task.use_chat_id and task.chat_id)
        chat_id: Optional[str] = None
        try:
            await self._client.get_state_instance()
            chat_id = task.chat_id if used_saved_chat else await self._resolve_chat(green_phone)
            send_result = await self._client.send_message(chat_id, text)
        except SendMessageRejectedError as exc:
            if not used_saved_chat or chat_id is None:
                logger.error(
                    "ADMIN_NOTIFY_SEND_FAILED error_code=%s detail=%s",
                    exc.error_code,
                    exc.message,
                )
                return None
            # The orchestrator supplied a saved chat_id that GREEN-API rejects
            # (typically a stale id from the old Playwright adapter, which used
            # a different id space). An explicit rejection means nothing was
            # sent, so resolving the real chat by phone and retrying once is
            # safe and cannot duplicate the notification. The corrected chat_id
            # is reported back, so the orchestrator updates its admin mapping.
            logger.warning(
                "ADMIN_NOTIFY_CHAT_ID_REJECTED saved_chat_id=%s detail=%s; "
                "resolving admin chat by phone",
                chat_id,
                exc.message,
            )
            try:
                chat_id = await self._resolve_chat(green_phone)
                send_result = await self._client.send_message(chat_id, text)
            except AdapterError as retry_exc:
                logger.error(
                    "ADMIN_NOTIFY_SEND_FAILED error_code=%s detail=%s",
                    retry_exc.error_code,
                    retry_exc.message,
                )
                return None
        except AdapterError as exc:
            logger.error(
                "ADMIN_NOTIFY_SEND_FAILED error_code=%s detail=%s",
                exc.error_code,
                exc.message,
            )
            return None

        logger.info(
            "ADMIN_NOTIFY_SENT tenant_id=%s chat_id=%s idMessage=%s",
            task.tenant_id,
            chat_id,
            send_result.id_message,
        )
        canonical_phone = to_go_canonical_phone(task.tenant_phone) or ""
        return TargetResult.for_admin_notify_sent(
            tenant_id=task.tenant_id,
            tenant_account_id=self._tenant_account_id,
            phone_number=canonical_phone,
            chat_id=chat_id,
        )

    async def _resolve_chat(self, green_phone: str) -> str:
        if self._client.check_account_rate_limited():
            raise CheckAccountRateLimitedError(
                "CheckAccount is in cooldown after a GREEN-API 469 rate limit"
            )
        result = await self._client.check_account(green_phone)
        if not result.exist or not result.chat_id:
            raise GreenApiError("admin phone not found on MAX")
        return result.chat_id


class IncomingMessagePoller:
    """Long-poll GREEN-API notifications and forward them to the orchestrator."""

    def __init__(
        self,
        *,
        tenant_account_id: str,
        client: GreenApiMaxClient,
        gateway,
        receive_timeout: int,
    ) -> None:
        self._tenant_account_id = tenant_account_id
        self._client = client
        self._gateway = gateway
        self._receive_timeout = receive_timeout
        self._stopped = asyncio.Event()

    def request_stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except GatewayStoppedError:
                return
            except AdapterError as exc:
                logger.warning(
                    "INCOMING_POLL_FAILED error_code=%s", exc.error_code
                )
                await self._wait(backoff)
                backoff = min(backoff * 2, 30.0)

    async def poll_once(self) -> int:
        """Receive one batch of notifications and process it."""
        notifications = await self._client.receive_notification(
            self._receive_timeout
        )
        if notifications:
            logger.info(
                "INCOMING_BATCH count=%d (%s)",
                len(notifications),
                "; ".join(notification_summary(n) for n in notifications),
            )
        for notification in notifications:
            await self._handle(notification)
        return len(notifications)

    async def _handle(self, notification) -> None:
        results = notification_to_results(
            notification, tenant_account_id=self._tenant_account_id
        )
        for result in results:
            await self._gateway.publish_result(result)

        receipt_id = (
            notification.get("receiptId") if isinstance(notification, dict) else None
        )
        if isinstance(receipt_id, int):
            # Delete AFTER the results are published so a crash re-delivers the
            # notification (Go deduplicates repeated replies).
            await self._client.delete_notification(receipt_id)

        if results:
            logger.info(
                "INCOMING_FORWARDED count=%d statuses=%s",
                len(results),
                ",".join(r.status for r in results),
            )
        else:
            logger.warning(
                "INCOMING_NOTIFICATION_IGNORED receipt=%s (%s)",
                receipt_id,
                notification_summary(notification),
            )

    async def _wait(self, seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)


class AdapterWorker:
    """Consume one account queue and drive :class:`TaskProcessor`."""

    def __init__(
        self,
        *,
        gateway,
        processor: TaskProcessor,
        admin_notify: Optional[AdminNotifyProcessor] = None,
        incoming_poller: Optional[IncomingMessagePoller] = None,
    ) -> None:
        self._gateway = gateway
        self._processor = processor
        self._admin_notify = admin_notify
        self._incoming_poller = incoming_poller
        self._stop_event = asyncio.Event()
        self._first_delivery_done = asyncio.Event()
        self._single_delivery_mode = False
        self._deliveries_started = 0

    # -- public entry points -------------------------------------------------

    async def run_forever(self) -> None:
        await self._gateway.connect()
        await self._gateway.start_consuming(self.handle_delivery)
        if self._admin_notify is not None:
            await self._gateway.start_consuming_notify(self.handle_notify_delivery)

        poll_task: Optional[asyncio.Task] = None
        if self._incoming_poller is not None:
            poll_task = asyncio.create_task(self._incoming_poller.run())

        try:
            await self._stop_event.wait()
        finally:
            if poll_task is not None:
                self._incoming_poller.request_stop()
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task
            await self._shutdown()

    async def run_once(self) -> None:
        """Process at most one delivery, then shut down cleanly."""
        self._single_delivery_mode = True
        await self._gateway.connect()
        await self._gateway.start_consuming(self.handle_delivery)
        try:
            await self._first_delivery_done.wait()
        finally:
            await self._shutdown()

    def request_stop(self) -> None:
        # Wake both run_forever and a --once run that is still waiting for its
        # first delivery, then abort any in-flight publish retry.
        self._stop_event.set()
        self._first_delivery_done.set()
        self._gateway.request_stop()
        if self._incoming_poller is not None:
            self._incoming_poller.request_stop()

    async def _shutdown(self) -> None:
        await self._gateway.stop_consuming()
        await self._gateway.close()

    # -- delivery handling ---------------------------------------------------

    async def handle_delivery(self, delivery) -> None:
        """Process one RabbitMQ delivery.

        Ack ordering: the original delivery is acked only *after* the result
        (or the dead-letter copy) has been confirmed by the broker.
        """
        # In --once mode, ignore any extra delivery that races in after the
        # first one: leave it un-acked so RabbitMQ redelivers it later.
        if self._single_delivery_mode:
            if self._deliveries_started >= 1:
                logger.info("RABBITMQ_ONCE_EXTRA_DELIVERY_IGNORED")
                return
            self._deliveries_started += 1

        try:
            task = SendTaskPayload.from_json(delivery.body)
        except InvalidTaskError as exc:
            await self._dead_letter(delivery, exc.reason)
            return

        result = await self._processor.process_task(task)
        # Raises GatewayStoppedError on shutdown: the delivery then stays
        # un-acked and will be redelivered (documented at-least-once window).
        await self._gateway.publish_result(result)
        await delivery.ack()
        if not self._first_delivery_done.is_set():
            self._first_delivery_done.set()

    async def _dead_letter(self, delivery, reason: str) -> None:
        logger.warning(
            "RABBITMQ_TASK_DEAD_LETTERED reason=%s", reason
        )
        await self._gateway.publish_dead_letter(delivery.body, reason)
        await delivery.ack()
        if not self._first_delivery_done.is_set():
            self._first_delivery_done.set()

    async def handle_notify_delivery(self, delivery) -> None:
        """Handle one tenant-admin notification task (its own queue)."""
        if self._admin_notify is None:
            await delivery.ack()
            return
        try:
            body = json.loads(delivery.body)
        except (ValueError, TypeError):
            logger.warning("RABBITMQ_NOTIFY_INVALID_JSON")
            await delivery.ack()
            return

        try:
            result = await self._admin_notify.process(body)
        except InvalidTaskError as exc:
            logger.warning("RABBITMQ_NOTIFY_INVALID_TASK reason=%s", exc.reason)
            await delivery.ack()
            return

        if result is not None:
            await self._gateway.publish_result(result)
        await delivery.ack()
