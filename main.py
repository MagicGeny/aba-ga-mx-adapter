#!/usr/bin/env python3
"""aba-ga-mx-adapter entry point.

Modes:

    python main.py --check-api   # validate config and call GetStateInstance only
    python main.py --once        # consume and process exactly one task, then exit
    python main.py               # long-running worker until SIGINT/SIGTERM
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from aba_ga_mx_adapter.config import AdapterConfig, configure_logging
from aba_ga_mx_adapter.errors import (
    AdapterError,
    ConfigError,
    ERROR_CODE_SESSION_EXPIRED,
)
from aba_ga_mx_adapter.green_api import GreenApiMaxClient
from aba_ga_mx_adapter.messaging import RabbitMqGateway
from aba_ga_mx_adapter.worker import (
    AdapterWorker,
    AdminNotifyProcessor,
    IncomingMessagePoller,
    TaskProcessor,
)

logger = logging.getLogger("aba_ga_mx_adapter.main")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_NOT_AUTHORIZED = 3

# One-off SetSettings payload for reply handling (used only by
# `--enable-incoming`; never called from the runtime worker).
SETTINGS_FOR_INCOMING = {
    # Notifications about inbound messages (user replies).
    "incomingWebhook": "yes",
    # Statuses (delivered/read) for messages we send.
    "outgoingWebhook": "yes",
    # Notifications for messages sent through the API.
    "outgoingAPIMessageWebhook": "yes",
}


def _build_client(config: AdapterConfig) -> GreenApiMaxClient:
    return GreenApiMaxClient(
        base_url=config.green_api_base_url,
        id_instance=config.green_api_id_instance,
        token_instance=config.green_api_token_instance,
        timeout_seconds=config.http_timeout_seconds,
    )


def _build_gateway(config: AdapterConfig) -> RabbitMqGateway:
    return RabbitMqGateway(
        rabbitmq_url=config.rabbitmq_url,
        send_queue=config.send_queue,
        dead_letter_queue=config.dead_letter_queue,
        results_queue=config.results_queue,
        notify_queue=config.notify_queue,
    )


async def _run_check_api(config: AdapterConfig) -> int:
    """Validate configuration and call GetStateInstance only (no RabbitMQ)."""
    async with _build_client(config) as client:
        try:
            state = await client.get_state_instance()
        except AdapterError as exc:
            print(
                f"GetStateInstance failed: {exc.message} "
                f"(error_code={exc.error_code})",
                file=sys.stderr,
            )
            if exc.error_code == ERROR_CODE_SESSION_EXPIRED:
                print(
                    "GREEN-API instance is NOT authorized; sending will be refused.",
                    file=sys.stderr,
                )
                return EXIT_NOT_AUTHORIZED
            return EXIT_ERROR

        print(f"stateInstance={state.state}")
        print("GREEN-API instance is authorized; configuration is valid.")

        # Notification settings decide whether inbound replies ever reach the
        # ReceiveNotification queue. Diagnose, but never modify here.
        try:
            settings = await client.get_settings()
        except AdapterError as exc:
            print(
                f"Could not read instance settings: {exc.message} "
                f"(error_code={exc.error_code})",
                file=sys.stderr,
            )
            print(
                "WARNING: incoming replies may not be received; verify the "
                "instance settings (incomingWebhook=yes, webhookUrl empty).",
                file=sys.stderr,
            )
            return EXIT_OK

        _print_notification_settings(settings)
    return EXIT_OK


def _print_notification_settings(settings: dict) -> None:
    webhook_url = str(settings.get("webhookUrl") or "").strip()
    incoming = str(settings.get("incomingWebhook") or "").strip().lower()
    outgoing = str(settings.get("outgoingWebhook") or "").strip().lower()

    print("Notification settings:")
    if webhook_url:
        print(
            f"  webhookUrl={webhook_url}  "
            "WARNING: with a webhook URL set, ReceiveNotification returns "
            "nothing; clear it to use this adapter's HTTP-API polling."
        )
    else:
        print("  webhookUrl=(empty)  OK for ReceiveNotification polling")

    if incoming == "yes":
        print("  incomingWebhook=yes  OK (user replies will be received)")
    else:
        print(
            "  incomingWebhook=%s  WARNING: user replies are NOT received; "
            "run `python main.py --enable-incoming` or enable "
            "'Получать уведомления о входящих сообщениях' in the GREEN-API "
            "console." % (incoming or "no")
        )

    if outgoing == "yes":
        print("  outgoingWebhook=yes  OK (delivered/read statuses will be received)")
    else:
        print(
            f"  outgoingWebhook={outgoing or 'no'}  "
            "WARNING: delivered/read statuses are NOT received."
        )


async def _run_enable_incoming(config: AdapterConfig) -> int:
    """One-off: enable the notification flags required for replies/statuses."""
    async with _build_client(config) as client:
        try:
            saved = await client.set_settings(SETTINGS_FOR_INCOMING)
        except AdapterError as exc:
            print(
                f"SetSettings failed: {exc.message} "
                f"(error_code={exc.error_code})",
                file=sys.stderr,
            )
            return EXIT_ERROR

    if not saved:
        print("SetSettings returned saveSettings=false", file=sys.stderr)
        return EXIT_ERROR

    print("Instance settings saved:")
    for key, value in SETTINGS_FOR_INCOMING.items():
        print(f"  {key}={value}")
    print(
        "GREEN-API restarts the instance on this call; settings apply within "
        "about 5 minutes."
    )
    print(
        "IMPORTANT: replies that users sent BEFORE this change were never "
        "queued — test with a NEW reply after the settings apply."
    )
    return EXIT_OK


async def _run_worker(config: AdapterConfig, *, once: bool) -> int:
    client = _build_client(config)
    gateway = _build_gateway(config)
    processor = TaskProcessor(
        tenant_account_id=config.tenant_account_id,
        client=client,
    )

    admin_notify = None
    incoming_poller = None
    if not once:
        admin_notify = AdminNotifyProcessor(
            tenant_account_id=config.tenant_account_id,
            client=client,
        )
        if config.poll_incoming_messages:
            incoming_poller = IncomingMessagePoller(
                tenant_account_id=config.tenant_account_id,
                client=client,
                gateway=gateway,
                receive_timeout=config.receive_timeout_seconds,
            )

    worker = AdapterWorker(
        gateway=gateway,
        processor=processor,
        admin_notify=admin_notify,
        incoming_poller=incoming_poller,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            # Not supported on this platform or not on the main thread.
            pass

    try:
        if once:
            await worker.run_once()
        else:
            await worker.run_forever()
    finally:
        await client.aclose()
    return EXIT_OK


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aba-ga-mx-adapter",
        description=(
            "RabbitMQ -> GREEN-API MAX text sender for one tenant_account_id."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-api",
        action="store_true",
        help="Validate configuration and call GetStateInstance only.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Consume and process exactly one task from RabbitMQ, then exit.",
    )
    mode.add_argument(
        "--enable-incoming",
        action="store_true",
        help=(
            "One-off: enable the GREEN-API notification flags required for "
            "incoming replies (incomingWebhook/outgoingWebhook/outgoingAPIMessageWebhook)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        config = AdapterConfig.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(config.log_level)
    logger.info(
        "ADAPTER_START tenant_account_id=%s send_queue=%s results_queue=%s",
        config.tenant_account_id,
        config.send_queue,
        config.results_queue,
    )

    try:
        if args.check_api:
            return asyncio.run(_run_check_api(config))
        if args.enable_incoming:
            return asyncio.run(_run_enable_incoming(config))
        return asyncio.run(_run_worker(config, once=args.once))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return EXIT_OK
    except AdapterError as exc:
        print(f"Adapter error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.error("ADAPTER_FATAL error=%s", type(exc).__name__)
        print(f"Fatal error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
