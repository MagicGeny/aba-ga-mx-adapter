"""Configuration loading and validation tests."""

from __future__ import annotations

import pytest

from aba_ga_mx_adapter.config import AdapterConfig
from aba_ga_mx_adapter.errors import ConfigError

TENANT_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"


def test_valid_configuration(valid_env):
    config = AdapterConfig.from_env(valid_env, load_env_file=False)

    assert config.tenant_account_id == TENANT_ACCOUNT_ID
    assert config.rabbitmq_url == "amqp://guest:guest@localhost:5672/"
    assert config.green_api_base_url == "https://example.green-api.com"
    assert config.green_api_id_instance == "1101000001"
    assert config.green_api_token_instance == "test-token-value"
    assert config.http_timeout_seconds == 30.0
    assert config.log_level == "INFO"


def test_derived_queue_names(valid_env, config):
    assert config.send_queue == f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}"
    assert (
        config.dead_letter_queue
        == f"tasks.messages.send.account.{TENANT_ACCOUNT_ID}.dead"
    )
    assert config.results_queue == "tasks.messages.results_replies_queue"
    assert (
        config.notify_queue
        == f"tasks.messages.tenant_admin_notify.account.{TENANT_ACCOUNT_ID}"
    )


def test_default_incoming_settings(valid_env):
    config = AdapterConfig.from_env(valid_env, load_env_file=False)
    assert config.receive_timeout_seconds == 10
    assert config.poll_incoming_messages is True


def test_incoming_settings_from_env(valid_env):
    valid_env["RECEIVE_TIMEOUT_SECONDS"] = "30"
    valid_env["POLL_INCOMING_MESSAGES"] = "false"
    config = AdapterConfig.from_env(valid_env, load_env_file=False)
    assert config.receive_timeout_seconds == 30
    assert config.poll_incoming_messages is False


def test_invalid_receive_timeout(valid_env):
    valid_env["RECEIVE_TIMEOUT_SECONDS"] = "120"
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


def test_trailing_slash_in_base_url_is_normalized(valid_env):
    valid_env["GREEN_API_BASE_URL"] = "https://example.green-api.com/"
    config = AdapterConfig.from_env(valid_env, load_env_file=False)
    assert config.green_api_base_url == "https://example.green-api.com"


def test_missing_tenant_account_id(valid_env):
    valid_env.pop("TENANT_ACCOUNT_ID")
    with pytest.raises(ConfigError) as exc:
        AdapterConfig.from_env(valid_env, load_env_file=False)
    assert "TENANT_ACCOUNT_ID" in str(exc.value)


def test_tenant_account_id_not_uuid(valid_env):
    valid_env["TENANT_ACCOUNT_ID"] = "not-a-uuid"
    with pytest.raises(ConfigError) as exc:
        AdapterConfig.from_env(valid_env, load_env_file=False)
    assert "UUID" in str(exc.value)


def test_tenant_account_id_nil_uuid_rejected(valid_env):
    valid_env["TENANT_ACCOUNT_ID"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.green-api.com",
        "ftp://example.green-api.com",
        "example.green-api.com",
        "https://",
    ],
)
def test_invalid_base_url(valid_env, value):
    valid_env["GREEN_API_BASE_URL"] = value
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


def test_http_instead_of_https_rejected(valid_env):
    valid_env["GREEN_API_BASE_URL"] = "http://example.green-api.com"
    with pytest.raises(ConfigError) as exc:
        AdapterConfig.from_env(valid_env, load_env_file=False)
    assert "https" in str(exc.value)


def test_missing_green_api_credentials(valid_env):
    valid_env.pop("GREEN_API_ID_INSTANCE")
    valid_env.pop("GREEN_API_TOKEN_INSTANCE")
    with pytest.raises(ConfigError) as exc:
        AdapterConfig.from_env(valid_env, load_env_file=False)
    message = str(exc.value)
    assert "GREEN_API_ID_INSTANCE" in message
    assert "GREEN_API_TOKEN_INSTANCE" in message


def test_default_rabbitmq_url(valid_env):
    valid_env.pop("RABBITMQ_URL")
    config = AdapterConfig.from_env(valid_env, load_env_file=False)
    assert config.rabbitmq_url == "amqp://guest:guest@localhost:5672/"


def test_invalid_rabbitmq_url(valid_env):
    valid_env["RABBITMQ_URL"] = "http://localhost:15672"
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


def test_default_timeout_and_log_level(valid_env):
    valid_env.pop("HTTP_TIMEOUT_SECONDS")
    valid_env.pop("LOG_LEVEL")
    config = AdapterConfig.from_env(valid_env, load_env_file=False)
    assert config.http_timeout_seconds == 30.0
    assert config.log_level == "INFO"


def test_invalid_timeout(valid_env):
    valid_env["HTTP_TIMEOUT_SECONDS"] = "zero"
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


def test_invalid_log_level(valid_env):
    valid_env["LOG_LEVEL"] = "CHATTY"
    with pytest.raises(ConfigError):
        AdapterConfig.from_env(valid_env, load_env_file=False)


def test_token_never_appears_in_error_messages(valid_env):
    """A missing/invalid value must not echo the token back."""
    token = "super-secret-token-value"
    valid_env["GREEN_API_TOKEN_INSTANCE"] = token
    valid_env["GREEN_API_BASE_URL"] = "http://example.green-api.com"

    with pytest.raises(ConfigError) as exc:
        AdapterConfig.from_env(valid_env, load_env_file=False)
    assert token not in str(exc.value)
