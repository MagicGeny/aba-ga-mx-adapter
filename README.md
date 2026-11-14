# aba-ga-mx-adapter (GREEN-API MAX)

Outbound **text** messaging to **MAX** via the **GREEN-API HTTP API**, driven by
the Go orchestrator over RabbitMQ.

This is the first working release of the adapter that replaces the old
`aba-mx-adapter` for outgoing text sends.

```
Go Orchestrator
      │  publish task
      ▼
RabbitMQ:  tasks.messages.send.account.<TENANT_ACCOUNT_ID>
      │  consume (prefetch=1, manual ack)
      ▼
aba-ga-mx-adapter
      │  GetStateInstance → (CheckAccount) → SendMessage
      ▼
RabbitMQ:  tasks.messages.results_replies_queue
      │
      ▼
Go ResultConsumer → campaign_targets.status = sent
```

One adapter process serves **one `tenant_account_id` and one GREEN-API
instance**.

## Layout

| Path | Purpose |
|---|---|
| `aba_ga_mx_adapter/config.py` | Configuration loading and validation |
| `aba_ga_mx_adapter/models.py` | `SendTaskPayload`, `TargetResult` |
| `aba_ga_mx_adapter/errors.py` | Statuses, error codes, typed errors |
| `aba_ga_mx_adapter/green_api/client.py` | GREEN-API HTTP client |
| `aba_ga_mx_adapter/messaging/rabbitmq.py` | RabbitMQ connect/consume/publish |
| `aba_ga_mx_adapter/worker.py` | Task processing + worker loop |
| `main.py` | CLI entry point |
| `docs/adapter-contract.md` | The exact Go contract |
| `scripts/green_api_smoke_test.py` | Legacy manual smoke test (not a worker) |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the values
```

### `.env`

```dotenv
# Sender account UUID from tenant_accounts.id (NOT the GREEN-API idInstance)
TENANT_ACCOUNT_ID=the_tenant_accounts_id_uuid

# RabbitMQ
RABBITMQ_URL=amqp://guest:guest@localhost:5672/

# GREEN-API MAX
GREEN_API_BASE_URL=https://your-instance.green-api.com
GREEN_API_ID_INSTANCE=your_id_instance
GREEN_API_TOKEN_INSTANCE=your_api_token

# HTTP
HTTP_TIMEOUT_SECONDS=30

# Incoming messages / replies
RECEIVE_TIMEOUT_SECONDS=10
POLL_INCOMING_MESSAGES=true

# Logging
LOG_LEVEL=INFO
```

`TENANT_ACCOUNT_ID` must be a UUID. `GREEN_API_BASE_URL` must be `https://`.
Secrets are never written to logs or exceptions, and `.env` is git-ignored.

## Running

```bash
# 1. Validate configuration and check the GREEN-API instance (no RabbitMQ).
python main.py --check-api

# 2. Process exactly one task, then exit (for the first real test).
python main.py --once

# 3. Long-running worker (until SIGINT/SIGTERM).
python main.py
```

Exit codes: `0` success, `1` runtime/API error, `2` configuration error,
`3` instance not authorized (`--check-api`).

`--once` sends nothing by itself: it waits for a real task on the
account-specific queue and processes that. `--check-api` only calls
`GetStateInstance` and never touches RabbitMQ.

## Queues and statuses

| Direction | Queue / exchange |
|---|---|
| Consume | `tasks.messages.send.account.<TENANT_ACCOUNT_ID>` (exchange `tasks.messages.direct`, key `account.<TENANT_ACCOUNT_ID>`) |
| Consume | `tasks.messages.tenant_admin_notify.account.<TENANT_ACCOUNT_ID>` |
| Publish results | `tasks.messages.results_replies_queue` (default exchange) |
| Dead-letter | `tasks.messages.send.account.<TENANT_ACCOUNT_ID>.dead` |

Statuses sent to Go: `sent`, `failed`, `user_not_found_by_phone`,
`delivery_unknown`, plus `replied` / `delivered` / `viewed` for inbound traffic.
See `docs/adapter-contract.md` for the full table of error codes and their retry
disposition.

### Phone numbers

The orchestrator sends phones as **10 digits starting with `9`**
(`domain.NormalizePhone`), e.g. `9601701985`. GREEN-API requires the
international form, so the adapter prepends `7` (`79601701985`) before calling
`CheckAccount`. Other shapes are rejected with `INVALID_PHONE`. The result's
`phone_number` always keeps the orchestrator's original value.

## Incoming messages (replies) and admin notifications

In long-running mode the adapter also:

* long-polls GREEN-API `ReceiveNotification` and forwards inbound user replies
  (`status=replied` + `reply_text`) and delivery statuses (`delivered` /
  `viewed`) to `tasks.messages.results_replies_queue`, so the orchestrator
  records the reply and drives the campaign status;
* consumes `tasks.messages.tenant_admin_notify.account.<TENANT_ACCOUNT_ID>` and
  sends the aggregated notification to the tenant admin via MAX.

### Required instance settings (otherwise replies are silently lost)

GREEN-API only queues a notification when the corresponding setting is enabled
(**after instance creation all of them are off**). For this adapter check or
enable:

| Setting | Needs to be | Why |
|---|---|---|
| `webhookUrl` | **empty** | with a webhook URL set, `ReceiveNotification` returns nothing |
| `incomingWebhook` | `yes` | user replies (`incomingMessageReceived`) |
| `outgoingWebhook` | `yes` | delivered/read statuses |
| `outgoingAPIMessageWebhook` | `yes` | notifications for API-sent messages |

Diagnose the current state:

```bash
python main.py --check-api        # prints stateInstance + notification settings
python main.py --enable-incoming  # one-off: enables the three webhook flags above
```

`--enable-incoming` calls `SetSettings` once (the instance restarts, settings
apply within ~5 minutes). It is never called by the runtime worker. Replies
sent **before** enabling were never queued — always test with a *new* reply.

## ⚠️ Before starting this adapter

Stop the old `aba-mx-adapter` for the **same `tenant_account_id`**. Both
processes would otherwise compete for messages on the same RabbitMQ queue
(`tasks.messages.send.account.<TENANT_ACCOUNT_ID>`), which can double-send or
steal tasks.

## First real test

1. Stop the old `aba-mx-adapter` for this account.
2. Make sure the account-specific queue has **no old tasks**.
3. Create a **separate campaign with a single test recipient**, cold contact
   (`use_chat_id=false`) so the `CheckAccount → SendMessage` path is exercised.
4. Run `python main.py --check-api`, then `python main.py --once`.
5. Verify in the orchestration logs that the target reached `sent`.

The first test must be a cold contact: compatibility of old Playwright
`chat_id` values with GREEN-API is not yet confirmed.

## Not implemented in this release

Attachment sending/receiving, `provider_message_id` in the Go result,
multi-account operation in a single process, and exactly-once sending. The
unavoidable at-least-once window is documented in `docs/adapter-contract.md`.

## Tests

```bash
python -m pytest tests/ -q
```

All tests are hermetic: GREEN-API HTTP is mocked (`respx`), RabbitMQ is mocked,
no real messages are sent, and the 2-hour `CheckAccount` cooldown is exercised
with an injected clock (no real waiting).
