"""CLI entry point tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import aio_pika
import pytest
import respx

import main as adapter_main

from tests.adapter_test_helpers import (
    CHAT_ID,
    FakeDelivery,
    FakeGateway,
    FakeGreenApiClient,
    TENANT_ACCOUNT_ID,
    task_bytes,
)

BASE = "https://example.green-api.com"
ID = "1101000001"
TOKEN = "test-token-value"
STATE_URL = f"{BASE}/waInstance{ID}/getStateInstance/{TOKEN}"
SETTINGS_URL = f"{BASE}/waInstance{ID}/getSettings/{TOKEN}"
SET_SETTINGS_URL = f"{BASE}/waInstance{ID}/setSettings/{TOKEN}"


def _mock_good_settings():
    return respx.get(SETTINGS_URL).respond(
        200,
        json={
            "webhookUrl": "",
            "incomingWebhook": "yes",
            "outgoingWebhook": "yes",
            "outgoingAPIMessageWebhook": "yes",
        },
    )

_ENV = {
    "TENANT_ACCOUNT_ID": TENANT_ACCOUNT_ID,
    "RABBITMQ_URL": "amqp://guest:guest@localhost:5672/",
    "GREEN_API_BASE_URL": BASE,
    "GREEN_API_ID_INSTANCE": ID,
    "GREEN_API_TOKEN_INSTANCE": TOKEN,
    "HTTP_TIMEOUT_SECONDS": "30",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def cli_env(monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    return _ENV


@pytest.fixture(autouse=True)
def no_rabbitmq(monkeypatch):
    """Fail loudly if a CLI path touches RabbitMQ when it should not."""
    connect = AsyncMock(side_effect=AssertionError("RabbitMQ must not be used"))
    monkeypatch.setattr(aio_pika, "connect_robust", connect)
    return connect


@respx.mock
def test_check_api_authorized_exits_zero(cli_env):
    respx.get(STATE_URL).respond(200, json={"stateInstance": "authorized"})
    _mock_good_settings()
    assert adapter_main.main(["--check-api"]) == adapter_main.EXIT_OK


@respx.mock
def test_check_api_not_authorized_exits_nonzero(cli_env):
    respx.get(STATE_URL).respond(200, json={"stateInstance": "notAuthorized"})
    assert adapter_main.main(["--check-api"]) == adapter_main.EXIT_NOT_AUTHORIZED


@respx.mock
def test_check_api_api_error_exits_error(cli_env):
    respx.get(STATE_URL).respond(500, json={"message": "boom"})
    assert adapter_main.main(["--check-api"]) == adapter_main.EXIT_ERROR


@respx.mock
def test_check_api_does_not_touch_rabbitmq(cli_env, no_rabbitmq):
    respx.get(STATE_URL).respond(200, json={"stateInstance": "authorized"})
    _mock_good_settings()
    adapter_main.main(["--check-api"])
    no_rabbitmq.assert_not_awaited()


@respx.mock
def test_check_api_warns_when_incoming_webhook_disabled(cli_env, capsys):
    respx.get(STATE_URL).respond(200, json={"stateInstance": "authorized"})
    respx.get(SETTINGS_URL).respond(
        200,
        json={"webhookUrl": "", "incomingWebhook": "no", "outgoingWebhook": "no"},
    )
    adapter_main.main(["--check-api"])
    output = capsys.readouterr().out
    assert "incomingWebhook" in output
    assert "WARNING" in output or "не получ" in output or "NOT received" in output


@respx.mock
def test_enable_incoming_sets_required_flags(cli_env, capsys):
    respx.get(STATE_URL).respond(200, json={"stateInstance": "authorized"})
    route = respx.post(SET_SETTINGS_URL).respond(200, json={"saveSettings": True})
    assert adapter_main.main(["--enable-incoming"]) == adapter_main.EXIT_OK

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "incomingWebhook": "yes",
        "outgoingWebhook": "yes",
        "outgoingAPIMessageWebhook": "yes",
    }
    output = capsys.readouterr().out
    assert "5 minutes" in output
    assert "NEW reply" in output


@respx.mock
def test_enable_incoming_api_error_exits_error(cli_env):
    respx.post(SET_SETTINGS_URL).respond(500, json={"message": "boom"})
    assert adapter_main.main(["--enable-incoming"]) == adapter_main.EXIT_ERROR


def test_missing_configuration_exits_config_error(monkeypatch):
    # Hermetic: ignore any real .env and clear the environment, so the test
    # does not depend on the developer's local .env file.
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    assert adapter_main.main([]) == adapter_main.EXIT_CONFIG


def test_invalid_tenant_account_id_exits_config_error(cli_env, monkeypatch):
    monkeypatch.setenv("TENANT_ACCOUNT_ID", "not-a-uuid")
    assert adapter_main.main(["--check-api"]) == adapter_main.EXIT_CONFIG


async def _wait_for_handler(gateway, attempts: int = 20) -> None:
    for _ in range(attempts):
        if gateway.handler is not None:
            return
        await asyncio.sleep(0)
    raise AssertionError("consumer handler was never registered")


async def test_run_worker_once_processes_one_task(monkeypatch, config):
    fake_client = FakeGreenApiClient()
    fake_gateway = FakeGateway()
    monkeypatch.setattr(adapter_main, "_build_client", lambda cfg: fake_client)
    monkeypatch.setattr(adapter_main, "_build_gateway", lambda cfg: fake_gateway)

    run = asyncio.create_task(adapter_main._run_worker(config, once=True))
    await _wait_for_handler(fake_gateway)
    await fake_gateway.handler(FakeDelivery(body=task_bytes()))
    return_code = await asyncio.wait_for(run, timeout=2)

    assert return_code == adapter_main.EXIT_OK
    assert len(fake_gateway.published_results) == 1
    assert fake_gateway.closed is True
    assert fake_client.closed is True
