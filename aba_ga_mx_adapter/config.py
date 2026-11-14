"""Configuration loading and validation.

Every required value is validated at start-up. Secrets (the GREEN-API token)
are never echoed back in error messages or logs.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

from .constants import (
    RESULTS_QUEUE,
    account_notify_queue,
    account_send_dead_letter_queue,
    account_send_queue,
)
from .errors import ConfigError

DEFAULT_RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_RECEIVE_TIMEOUT_SECONDS = 10

# GREEN-API ReceiveNotification receiveTimeout documented range (seconds).
RECEIVE_TIMEOUT_MIN = 5
RECEIVE_TIMEOUT_MAX = 60

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# The nil UUID is never a valid sender account: it means the tenant_account_id
# placeholder was not filled in.
_NIL_UUID = uuid.UUID(int=0)


@dataclass(frozen=True)
class AdapterConfig:
    """Validated adapter configuration."""

    tenant_account_id: str
    rabbitmq_url: str
    green_api_base_url: str
    green_api_id_instance: str
    green_api_token_instance: str
    http_timeout_seconds: float
    log_level: str
    receive_timeout_seconds: int = DEFAULT_RECEIVE_TIMEOUT_SECONDS
    poll_incoming_messages: bool = True

    # -- Derived queue names -------------------------------------------------

    @property
    def send_queue(self) -> str:
        return account_send_queue(self.tenant_account_id)

    @property
    def dead_letter_queue(self) -> str:
        return account_send_dead_letter_queue(self.tenant_account_id)

    @property
    def notify_queue(self) -> str:
        return account_notify_queue(self.tenant_account_id)

    @property
    def results_queue(self) -> str:
        return RESULTS_QUEUE

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        load_env_file: bool = True,
    ) -> "AdapterConfig":
        """Build and validate the configuration.

        When ``env`` is not supplied, ``.env`` is loaded (without overriding
        real environment variables) and ``os.environ`` is used.
        """
        if env is None:
            if load_env_file:
                try:
                    from dotenv import load_dotenv

                    load_dotenv(override=False)
                except ImportError:  # pragma: no cover - python-dotenv is a dependency
                    pass
            env = os.environ  # type: ignore[assignment]

        missing: list[str] = []

        raw_tenant_account_id = _get(env, "TENANT_ACCOUNT_ID")
        raw_rabbitmq_url = _get(env, "RABBITMQ_URL")
        raw_base_url = _get(env, "GREEN_API_BASE_URL")
        raw_id_instance = _get(env, "GREEN_API_ID_INSTANCE")
        raw_token = _get(env, "GREEN_API_TOKEN_INSTANCE")
        raw_timeout = _get(env, "HTTP_TIMEOUT_SECONDS")
        raw_log_level = _get(env, "LOG_LEVEL")
        raw_receive_timeout = _get(env, "RECEIVE_TIMEOUT_SECONDS")
        raw_poll_incoming = _get(env, "POLL_INCOMING_MESSAGES")

        if not raw_tenant_account_id:
            missing.append("TENANT_ACCOUNT_ID")
        if not raw_base_url:
            missing.append("GREEN_API_BASE_URL")
        if not raw_id_instance:
            missing.append("GREEN_API_ID_INSTANCE")
        if not raw_token:
            missing.append("GREEN_API_TOKEN_INSTANCE")

        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill in all values."
            )

        tenant_account_id = _parse_tenant_account_id(raw_tenant_account_id)
        base_url = _validate_base_url(raw_base_url)
        rabbitmq_url = _validate_rabbitmq_url(raw_rabbitmq_url or DEFAULT_RABBITMQ_URL)
        timeout = _parse_timeout(raw_timeout)
        log_level = _parse_log_level(raw_log_level)

        return cls(
            tenant_account_id=tenant_account_id,
            rabbitmq_url=rabbitmq_url,
            green_api_base_url=base_url,
            green_api_id_instance=raw_id_instance.strip(),
            green_api_token_instance=raw_token.strip(),
            http_timeout_seconds=timeout,
            log_level=log_level,
            receive_timeout_seconds=_parse_receive_timeout(raw_receive_timeout),
            poll_incoming_messages=_parse_bool(raw_poll_incoming, default=True),
        )


def _get(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _parse_tenant_account_id(raw: str) -> str:
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ConfigError(
            "TENANT_ACCOUNT_ID must be a valid UUID (tenant_accounts.id). "
            f"Got: {raw!r}"
        ) from None
    if parsed == _NIL_UUID:
        raise ConfigError(
            "TENANT_ACCOUNT_ID must not be the nil UUID; set the real "
            "tenant_accounts.id of this adapter process."
        )
    return str(parsed)


def _validate_base_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ConfigError(
            "GREEN_API_BASE_URL must use https:// (plain http:// is not allowed). "
            f"Got scheme: {parsed.scheme or '(none)'!r}"
        )
    if not parsed.netloc:
        raise ConfigError(
            "GREEN_API_BASE_URL must be a valid absolute URL, "
            f"e.g. https://example.green-api.com. Got: {value!r}"
        )
    # Trailing slashes are tolerated: the URL is normalized here.
    return value.rstrip("/")


def _validate_rabbitmq_url(raw: str) -> str:
    value = raw.strip()
    if not (value.startswith("amqp://") or value.startswith("amqps://")):
        raise ConfigError(
            "RABBITMQ_URL must start with amqp:// or amqps://. "
            f"Got scheme: {urlparse(value).scheme or '(none)'!r}"
        )
    return value


def _parse_timeout(raw: str) -> float:
    if not raw:
        return DEFAULT_HTTP_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        raise ConfigError(
            f"HTTP_TIMEOUT_SECONDS must be a number. Got: {raw!r}"
        ) from None
    if timeout <= 0:
        raise ConfigError(
            f"HTTP_TIMEOUT_SECONDS must be positive. Got: {timeout!r}"
        )
    return timeout


def _parse_receive_timeout(raw: str) -> int:
    if not raw:
        return DEFAULT_RECEIVE_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"RECEIVE_TIMEOUT_SECONDS must be an integer. Got: {raw!r}"
        ) from None
    if value < RECEIVE_TIMEOUT_MIN or value > RECEIVE_TIMEOUT_MAX:
        raise ConfigError(
            f"RECEIVE_TIMEOUT_SECONDS must be between {RECEIVE_TIMEOUT_MIN} "
            f"and {RECEIVE_TIMEOUT_MAX}. Got: {value}"
        )
    return value


def _parse_bool(raw: str, *, default: bool) -> bool:
    if not raw:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Expected a boolean (true/false). Got: {raw!r}")


def _parse_log_level(raw: str) -> str:
    if not raw:
        return DEFAULT_LOG_LEVEL
    level = raw.strip().upper()
    if level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            "LOG_LEVEL must be one of "
            + ", ".join(sorted(_VALID_LOG_LEVELS))
            + f". Got: {raw!r}"
        )
    return level


def configure_logging(level: str) -> None:
    """Configure root logging for the adapter process."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
