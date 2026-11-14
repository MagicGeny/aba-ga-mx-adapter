"""RabbitMQ gateway and worker-loop tests.

The real ``RabbitMqGateway`` is exercised with a mocked ``aio_pika``; the
worker's ack ordering is checked against a recording fake gateway.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aio_pika
import pytest
from aio_pika import DeliveryMode

from aba_ga_mx_adapter.errors import (
    ERROR_CODE_INTERNAL_ERROR,
    STATUS_FAILED,
    STATUS_SENT,
)
from aba_ga_mx_adapter.messaging import GatewayStoppedError, RabbitMqGateway
from aba_ga_mx_adapter.models import SendTaskPayload, TargetResult
from aba_ga_mx_adapter.worker import AdapterWorker, TaskProcessor

from tests.adapter_test_helpers import (
    CHAT_ID,
    FakeDelivery,
    FakeGateway,
    FakeGreenApiClient,
    TENANT_ACCOUNT_ID,
    task_bytes,
)

RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"
SEND_QUEUE = f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}"
DEAD_QUEUE = f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}.dead"
NOTIFY_QUEUE = f"tasks.messages.tenant_admin_notify.account.{TENANT_ACCOUNT_ID}"
RESULTS_QUEUE = "tasks.messages.results_replies_queue"


class _RabbitMqMock:
    def __init__(self) -> None:
        self.channel = AsyncMock()
        self.channel.is_closed = False
        self.channel.set_qos = AsyncMock()
        self.channel.close = AsyncMock()
        self.declared: dict[str, AsyncMock] = {}
        self.channel.declare_queue = AsyncMock(side_effect=self._declare_queue)
        self.default_exchange = MagicMock()
        self.default_exchange.publish = AsyncMock()
        self.channel.default_exchange = self.default_exchange
        self.connection = AsyncMock()
        self.connection.is_closed = False
        self.connection.channel = AsyncMock(return_value=self.channel)
        self.connection.close = AsyncMock()

    def _declare_queue(self, name, durable=False, exclusive=False, auto_delete=False):
        queue = AsyncMock()
        queue.name = name
        queue.consume = AsyncMock(return_value="consumer-tag")
        queue.cancel = AsyncMock()
        self.declared[name] = queue
        return queue


def make_gateway() -> RabbitMqGateway:
    return RabbitMqGateway(
        rabbitmq_url=RABBITMQ_URL,
        send_queue=SEND_QUEUE,
        dead_letter_queue=DEAD_QUEUE,
        results_queue=RESULTS_QUEUE,
        notify_queue=NOTIFY_QUEUE,
    )


@pytest.fixture
def rabbitmq_mock(monkeypatch):
    mock = _RabbitMqMock()
    connect = AsyncMock(return_value=mock.connection)
    monkeypatch.setattr(aio_pika, "connect_robust", connect)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    return mock


def sample_result() -> TargetResult:
    return TargetResult.sent(
        SendTaskPayload.from_json(task_bytes()), chat_id=CHAT_ID
    )


# ---------------------------------------------------------------------------
# Gateway connection / queue declaration
# ---------------------------------------------------------------------------


async def test_connect_declares_account_queues_durable(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()

    aio_pika.connect_robust.assert_awaited_once_with(RABBITMQ_URL)
    rabbitmq_mock.channel.set_qos.assert_awaited_once_with(prefetch_count=1)

    for name in (SEND_QUEUE, DEAD_QUEUE, RESULTS_QUEUE, NOTIFY_QUEUE):
        rabbitmq_mock.channel.declare_queue.assert_any_await(
            name, durable=True, exclusive=False, auto_delete=False
        )


async def test_gateway_uses_exact_account_specific_queue_names():
    gateway = make_gateway()
    assert gateway.send_queue_name == f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}"
    assert (
        gateway.dead_letter_queue_name
        == f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}.dead"
    )
    assert gateway.results_queue_name == "tasks.messages.results_replies_queue"
    assert (
        gateway.notify_queue_name
        == f"tasks.messages.tenant_admin_notify.account.{TENANT_ACCOUNT_ID}"
    )


async def test_consume_uses_manual_ack(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    handler = AsyncMock()
    await gateway.start_consuming(handler)

    queue = rabbitmq_mock.declared[SEND_QUEUE]
    queue.consume.assert_awaited_once_with(handler, no_ack=False)


async def test_consume_notify_queue(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    handler = AsyncMock()
    await gateway.start_consuming_notify(handler)

    queue = rabbitmq_mock.declared[NOTIFY_QUEUE]
    queue.consume.assert_awaited_once_with(handler, no_ack=False)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


async def test_publish_result_to_results_queue_persistent_json(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    result = sample_result()

    await gateway.publish_result(result)

    publish = rabbitmq_mock.default_exchange.publish
    publish.assert_awaited_once()
    message = publish.await_args.args[0]
    assert publish.await_args.kwargs["routing_key"] == RESULTS_QUEUE
    assert message.body == result.to_json()
    assert message.properties.content_type == "application/json"
    assert message.properties.delivery_mode == DeliveryMode.PERSISTENT


async def test_publish_result_retries_until_confirmed(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    publish = rabbitmq_mock.default_exchange.publish
    publish.side_effect = [RuntimeError("nack"), None]

    await gateway.publish_result(sample_result())

    assert publish.await_count == 2


async def test_publish_dead_letter_carries_reason_header(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()

    await gateway.publish_dead_letter(b'{"broken": true}', "invalid_json")

    publish = rabbitmq_mock.default_exchange.publish
    message = publish.await_args.args[0]
    assert publish.await_args.kwargs["routing_key"] == DEAD_QUEUE
    assert message.body == b'{"broken": true}'
    assert message.headers["x-aba-dead-letter-reason"] == "invalid_json"


async def test_publish_aborted_on_stop_request(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    gateway.request_stop()

    with pytest.raises(GatewayStoppedError):
        await gateway.publish_result(sample_result())


async def test_close_closes_channel_and_connection(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    await gateway.close()

    rabbitmq_mock.channel.close.assert_awaited_once()
    rabbitmq_mock.connection.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Worker ack ordering (fake gateway)
# ---------------------------------------------------------------------------


def make_worker(gateway, client=None) -> tuple[AdapterWorker, FakeGreenApiClient]:
    client = client or FakeGreenApiClient()
    processor = TaskProcessor(tenant_account_id=TENANT_ACCOUNT_ID, client=client)
    worker = AdapterWorker(gateway=gateway, processor=processor)
    return worker, client


async def test_result_published_before_ack():
    gateway = FakeGateway()
    worker, client = make_worker(gateway)
    delivery = FakeDelivery(body=task_bytes())

    await worker.handle_delivery(delivery)

    assert delivery.acked is True
    assert len(gateway.published_results) == 1
    assert gateway.published_results[0].status == STATUS_SENT
    assert len(client.send_calls) == 1


async def test_no_ack_when_result_publish_fails():
    gateway = FakeGateway()
    gateway.fail_publish_attempts = 1
    worker, client = make_worker(gateway)
    delivery = FakeDelivery(body=task_bytes())

    with pytest.raises(RuntimeError):
        await worker.handle_delivery(delivery)

    assert delivery.acked is False
    # SendMessage is not called twice for the same delivery.
    assert len(client.send_calls) == 1


async def test_real_gateway_retries_publish_without_resending(rabbitmq_mock):
    gateway = make_gateway()
    await gateway.connect()
    publish = rabbitmq_mock.default_exchange.publish
    publish.side_effect = [RuntimeError("nack"), None]

    worker, client = make_worker(gateway)
    delivery = FakeDelivery(body=task_bytes())

    await worker.handle_delivery(delivery)

    assert publish.await_count == 2
    assert len(client.send_calls) == 1
    assert delivery.acked is True


async def test_invalid_task_is_dead_lettered_and_acked():
    gateway = FakeGateway()
    worker, client = make_worker(gateway)
    delivery = FakeDelivery(body=b"{not json")

    await worker.handle_delivery(delivery)

    assert len(gateway.published_results) == 0
    assert len(gateway.dead_letters) == 1
    body, reason = gateway.dead_letters[0]
    assert body == b"{not json"
    assert reason
    assert delivery.acked is True
    assert client.state_calls == 0


async def test_dead_letter_publish_failure_does_not_ack():
    gateway = FakeGateway()
    gateway.fail_dead_letter_attempts = 1
    worker, _ = make_worker(gateway)
    delivery = FakeDelivery(body=b"nope")

    with pytest.raises(RuntimeError):
        await worker.handle_delivery(delivery)

    assert delivery.acked is False


# ---------------------------------------------------------------------------
# run_once / run_forever
# ---------------------------------------------------------------------------


async def _wait_for_handler(gateway, attempts: int = 20) -> None:
    for _ in range(attempts):
        if gateway.handler is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("consumer handler was never registered")


async def test_run_once_processes_at_most_one_delivery():
    gateway = FakeGateway()
    worker, client = make_worker(gateway)

    run = asyncio.create_task(worker.run_once())
    await _wait_for_handler(gateway)

    await gateway.handler(FakeDelivery(body=task_bytes()))
    await run

    assert len(gateway.published_results) == 1
    assert len(client.send_calls) == 1
    assert gateway.closed is True
    assert gateway.stopped_consuming is True


async def test_run_once_ignores_extra_racing_delivery():
    gateway = FakeGateway()
    worker, client = make_worker(gateway)

    run = asyncio.create_task(worker.run_once())
    await _wait_for_handler(gateway)

    first = FakeDelivery(body=task_bytes())
    second = FakeDelivery(body=task_bytes())
    await gateway.handler(first)
    await gateway.handler(second)
    await run

    assert len(gateway.published_results) == 1
    assert first.acked is True
    # The racing delivery is left un-acked so it is redelivered later.
    assert second.acked is False


async def test_run_once_exits_on_stop_request_without_delivery():
    gateway = FakeGateway()
    worker, _ = make_worker(gateway)

    run = asyncio.create_task(worker.run_once())
    await _wait_for_handler(gateway)

    worker.request_stop()
    await asyncio.wait_for(run, timeout=2)

    assert gateway.published_results == []
    assert gateway.closed is True
    assert gateway.stopped_consuming is True


async def test_run_forever_stops_on_request_and_closes():
    gateway = FakeGateway()
    worker, _ = make_worker(gateway)

    run = asyncio.create_task(worker.run_forever())
    await _wait_for_handler(gateway)

    worker.request_stop()
    await asyncio.wait_for(run, timeout=2)

    assert gateway.stop_requested is True
    assert gateway.closed is True
    assert gateway.stopped_consuming is True
