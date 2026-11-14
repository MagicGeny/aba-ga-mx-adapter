"""RabbitMQ gateway: connect, consume, publish results, dead-letter.

Contract with the Go orchestrator (verified against
``aba-go-orchestrator/internal/worker/outbox.go`` and
``aba-go-orchestrator/cmd/orchestrator/main.go``):

* consume ``tasks.messages.send.account.<TENANT_ACCOUNT_ID>`` (durable,
  non-exclusive, non-auto-delete, prefetch=1, manual ack);
* publish results to ``tasks.messages.results_replies_queue`` over the default
  exchange (routing key == queue name), persistent, ``application/json``,
  with publisher confirms;
* publish unparsable tasks to
  ``tasks.messages.send.account.<TENANT_ACCOUNT_ID>.dead``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import aio_pika
from aio_pika import DeliveryMode, IncomingMessage, Message

from ..models import TargetResult

logger = logging.getLogger(__name__)

_PUBLISH_INITIAL_BACKOFF_SECONDS = 1.0
_PUBLISH_MAX_BACKOFF_SECONDS = 30.0

DEAD_LETTER_REASON_HEADER = "x-aba-dead-letter-reason"


class GatewayStoppedError(Exception):
    """Raised when a publish retry loop is aborted by a shutdown request."""


class RabbitMqGateway:
    """Owns the RabbitMQ connection, channels and queue declarations."""

    def __init__(
        self,
        *,
        rabbitmq_url: str,
        send_queue: str,
        dead_letter_queue: str,
        results_queue: str,
        notify_queue: str,
    ) -> None:
        self._rabbitmq_url = rabbitmq_url
        self._send_queue_name = send_queue
        self._dead_letter_queue_name = dead_letter_queue
        self._results_queue_name = results_queue
        self._notify_queue_name = notify_queue

        self._connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self._channel: Optional[aio_pika.abc.AbstractChannel] = None
        self._send_queue: Optional[aio_pika.abc.AbstractQueue] = None
        self._dead_letter_queue: Optional[aio_pika.abc.AbstractQueue] = None
        self._results_queue: Optional[aio_pika.abc.AbstractQueue] = None
        self._notify_queue: Optional[aio_pika.abc.AbstractQueue] = None

        self._consumer_tag: Optional[str] = None
        self._notify_consumer_tag: Optional[str] = None
        self._stop_requested = False

    # -- properties (used by tests) -----------------------------------------

    @property
    def send_queue_name(self) -> str:
        return self._send_queue_name

    @property
    def dead_letter_queue_name(self) -> str:
        return self._dead_letter_queue_name

    @property
    def results_queue_name(self) -> str:
        return self._results_queue_name

    @property
    def notify_queue_name(self) -> str:
        return self._notify_queue_name

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._rabbitmq_url)
        # publisher_confirms=True: publish() resolves only after the broker ack.
        self._channel = await self._connection.channel(publisher_confirms=True)
        # One task at a time per process.
        await self._channel.set_qos(prefetch_count=1)

        self._send_queue = await self._declare(self._send_queue_name)
        self._dead_letter_queue = await self._declare(self._dead_letter_queue_name)
        self._results_queue = await self._declare(self._results_queue_name)
        self._notify_queue = await self._declare(self._notify_queue_name)

        logger.info(
            "RABBITMQ_CONNECTED host=%s send_queue=%s results_queue=%s "
            "dead_letter_queue=%s notify_queue=%s",
            _safe_host(self._rabbitmq_url),
            self._send_queue_name,
            self._results_queue_name,
            self._dead_letter_queue_name,
            self._notify_queue_name,
        )

    async def _declare(self, name: str):
        assert self._channel is not None
        # Same parameters as the Go orchestrator's QueueDeclare(name, true,
        # false, false, false, nil).
        return await self._channel.declare_queue(
            name,
            durable=True,
            exclusive=False,
            auto_delete=False,
        )

    async def close(self) -> None:
        if self._channel is not None and not self._channel.is_closed:
            try:
                await self._channel.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                logger.warning("RABBITMQ_CHANNEL_CLOSE_FAILED")
        if self._connection is not None and not self._connection.is_closed:
            try:
                await self._connection.close()
            except Exception:  # pragma: no cover - best effort on shutdown
                logger.warning("RABBITMQ_CONNECTION_CLOSE_FAILED")
        self._channel = None
        self._connection = None
        self._send_queue = None
        self._dead_letter_queue = None
        self._results_queue = None
        self._notify_queue = None
        self._consumer_tag = None
        self._notify_consumer_tag = None

    # -- consuming -----------------------------------------------------------

    async def start_consuming(
        self, handler: Callable[[IncomingMessage], Awaitable[None]]
    ) -> None:
        assert self._send_queue is not None
        self._consumer_tag = await self._send_queue.consume(handler, no_ack=False)

    async def start_consuming_notify(
        self, handler: Callable[[IncomingMessage], Awaitable[None]]
    ) -> None:
        assert self._notify_queue is not None
        self._notify_consumer_tag = await self._notify_queue.consume(
            handler, no_ack=False
        )

    async def stop_consuming(self) -> None:
        if self._send_queue is not None and self._consumer_tag is not None:
            try:
                await self._send_queue.cancel(self._consumer_tag)
            except Exception:  # pragma: no cover - best effort on shutdown
                logger.warning("RABBITMQ_CONSUMER_CANCEL_FAILED")
        if self._notify_queue is not None and self._notify_consumer_tag is not None:
            try:
                await self._notify_queue.cancel(self._notify_consumer_tag)
            except Exception:  # pragma: no cover - best effort on shutdown
                logger.warning("RABBITMQ_NOTIFY_CONSUMER_CANCEL_FAILED")
        self._consumer_tag = None
        self._notify_consumer_tag = None

    def request_stop(self) -> None:
        """Abort in-flight publish retries (graceful shutdown)."""
        self._stop_requested = True

    # -- publishing ----------------------------------------------------------

    async def publish_result(self, result: TargetResult) -> None:
        """Publish a result; only returns after the broker confirms it."""
        message = Message(
            body=result.to_json(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
        )
        await self._publish_with_retry(
            message,
            routing_key=self._results_queue_name,
            description="result",
        )

    async def publish_dead_letter(self, raw_body: bytes, reason: str) -> None:
        """Publish an unparsable task to the account dead-letter queue."""
        message = Message(
            body=raw_body,
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers={DEAD_LETTER_REASON_HEADER: reason[:256]},
        )
        await self._publish_with_retry(
            message,
            routing_key=self._dead_letter_queue_name,
            description="dead_letter",
        )

    async def _publish_with_retry(
        self, message: Message, *, routing_key: str, description: str
    ) -> None:
        """Publish over the default exchange, retrying until confirmed.

        The Go orchestrator uses the default exchange with the queue name as
        the routing key. A failed publish is retried (never dropped) so the
        already-formed result is not lost; the original delivery stays
        un-acked meanwhile.
        """
        assert self._channel is not None
        delay = _PUBLISH_INITIAL_BACKOFF_SECONDS
        attempt = 0
        while True:
            if self._stop_requested:
                raise GatewayStoppedError(
                    f"stopped before {description} was confirmed"
                )
            try:
                await self._channel.default_exchange.publish(
                    message, routing_key=routing_key
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                logger.warning(
                    "RABBITMQ_PUBLISH_RETRY kind=%s routing_key=%s attempt=%d error=%s",
                    description,
                    routing_key,
                    attempt,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _PUBLISH_MAX_BACKOFF_SECONDS)


def _safe_host(rabbitmq_url: str) -> str:
    """Return host:port only, so credentials never reach the logs."""
    parsed = urlparse(rabbitmq_url)
    host = parsed.hostname or "unknown"
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host
