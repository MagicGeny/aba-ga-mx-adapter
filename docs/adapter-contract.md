# aba-ga-mx-adapter ↔ Orchestrator Contract

This document describes the **implemented** contract between the Python
`aba-ga-mx-adapter` and the existing Go orchestrator
(`MagicGeny/aba-go-orchestrator`).

It replaces the earlier "contract + smoke test only" draft. Everything below
was verified against the current Go sources:

* `internal/domain/models.go` — `SendTaskPayload`, `TargetResult`, `TaskStatus`
* `internal/domain/errors.go` — structured error codes and retry disposition
* `internal/worker/outbox.go` — send queue, exchange, routing key
* `internal/worker/campaign_doser.go` — `scheduleTarget()` payload fields
* `internal/worker/result_consumer.go` — result handling
* `cmd/orchestrator/main.go` — results queue name

---

## 1. Corrections to the earlier draft

| Earlier (incorrect) | Actual contract |
|---|---|
| `account_id` = GREEN-API `idInstance` | `account_id` is the **sender account UUID** (`tenant_accounts.id`), identical to `tenant_account_id`. It is **not** the GREEN-API instance. |
| `tenant_account_id` = some other identity | `tenant_account_id` **is** the same sender-account UUID (`tenant_accounts.id`). |
| `messenger` = `"MAX"`, `messenger_type` = `"v3"` | `messenger` = `"max"` (lowercase), `messenger_type` = `"MAX"`. They differ. |
| Attachments as an `attachments` array | Attachments are the top-level fields `attachment_url` and `attachment_name`. |
| `task_id` generic id | `task_id` is the `campaign_targets.id` UUID and is the future `target_id`. |
| `provider_message_id` proposed in the result | **Not** part of the current Go `TargetResult`; it is only written to a log. Adding it requires a coordinated Go change. |

The GREEN-API `idInstance` / `apiTokenInstance` are **only** configuration of
the Python process (`GREEN_API_ID_INSTANCE` / `GREEN_API_TOKEN_INSTANCE`) and
never travel through RabbitMQ.

---

## 2. Outgoing task contract

Published by the Go orchestrator (`CampaignDoser.scheduleTarget`) to
`tasks.messages.send.account.<tenant_account_id>`.

| JSON field | Type | Meaning |
|---|---|---|
| `task_id` | UUID string | `campaign_targets.id`; equals the result `target_id`. |
| `campaign_id` | UUID string | Campaign id. |
| `tenant_id` | UUID string | Tenant id. |
| `tenant_account_id` | UUID string | Sender account (`tenant_accounts.id`). |
| `account_id` | UUID string | Same UUID as `tenant_account_id` (routing identity). |
| `messenger` | string | `"max"` (lowercase). |
| `messenger_type` | string | `"MAX"`. |
| `phone` | string | Recipient number, digits only. The Go orchestrator normalizes it to **10 digits starting with `9`** (`domain.NormalizePhone`, applied in `usecase/campaign.go`). The adapter converts it to GREEN-API's international form before calling `CheckAccount` (see §2 phone details below and §7). |
| `message_text` | string | Final message text (spintax already expanded by Go). |
| `use_chat_id` | bool | When `true`, send directly to `chat_id`. |
| `chat_id` | string | Saved MAX chat id; required when `use_chat_id=true`. |
| `contact_type` | string | `"cold"` or `"warm"`. |
| `attachment_url` | string? | Optional. **Unsupported in this release.** |
| `attachment_name` | string? | Optional. **Unsupported in this release.** |

### Pre-send validation (before any GREEN-API call)

1. All four required UUIDs parse.
2. `tenant_account_id` equals `TENANT_ACCOUNT_ID` of this process.
3. `account_id` equals `tenant_account_id`.
4. `messenger_type` is `MAX` (case-insensitive).
5. No attachment is present.
6. `message_text` is non-empty and at most 4000 characters.
7. If `use_chat_id=true`, `chat_id` is non-empty.

Any failure here produces a `failed` result with a diagnostic `error_code`; the
GREEN-API is never called.

---

## 3. Result contract

Published by the adapter to `tasks.messages.results_replies_queue`. The JSON
contains exactly the fields of the current Go `TargetResult` (plus the optional
`reply_text`, which the send adapter never sets).

```json
{
  "target_id": "01234567-89ab-cdef-0123-456789abcdef",
  "campaign_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "tenant_id": "99999999-8888-7777-6666-555555555555",
  "tenant_account_id": "11111111-2222-3333-4444-555555555555",
  "account_id": "11111111-2222-3333-4444-555555555555",
  "phone_number": "79991234567",
  "status": "sent",
  "error_code": "",
  "error_message": null,
  "timestamp": "2026-11-12T12:00:00.000Z",
  "chat_id": "10000000",
  "messenger_type": "MAX"
}
```

* `target_id` = incoming `task_id`.
* `account_id` = sender-account UUID (same as `tenant_account_id`).
* `phone_number` = incoming `phone`.
* `chat_id` = the GREEN-API chat id actually used, else `""`.
* `timestamp` = current UTC time, ISO-8601 (`...Z`).
* `error_code` = `""` on success; `error_message` = `null` on success.
* **No `provider_message_id`.** The GREEN-API `idMessage` is written to the
  adapter log only, for diagnostics.

### Statuses

| Status | When |
|---|---|
| `sent` | `SendMessage` returned an `idMessage` (also the admin-notification result). Delivery/read status is **not** implied. |
| `failed` | Any final error (validation, explicit API rejection, instance restriction, internal error). |
| `user_not_found_by_phone` | `CheckAccount` returned `exist=false`. |
| `delivery_unknown` | `SendMessage` outcome is unknown (timeout / transport error after dispatch). Go does **not** retry these. |
| `replied` | Inbound user reply (carries `reply_text`); see §5. |
| `delivered` / `viewed` | `outgoingMessageStatus` `delivered` / `read`; see §5. |

### Error codes

Go-known codes (and their retry disposition in `internal/domain/errors.go`):

| Code | Go disposition |
|---|---|
| `USER_NOT_FOUND_BY_PHONE` | final |
| `INVALID_PHONE` | final |
| `ACCOUNT_MISMATCH` | final |
| `SESSION_EXPIRED` | retryable |
| `WORKER_UNAVAILABLE` | retryable |
| `DELIVERY_UNKNOWN` | unknown (no auto retry) |

Adapter-internal diagnostic codes (unknown to Go → treated as **final**):

| Code | When |
|---|---|
| `UNSUPPORTED_ATTACHMENT` | `attachment_url`/`attachment_name` present. |
| `CHECK_ACCOUNT_RATE_LIMITED` | GREEN-API `469` on `checkAccount`; a 2-hour cooldown starts. |
| `UNSUPPORTED_MESSENGER_TYPE` | `messenger_type` is not `MAX`. |
| `INVALID_MESSAGE_TEXT` | `message_text` empty or longer than 4000 characters. |
| `MISSING_CHAT_ID` | `use_chat_id=true` with an empty `chat_id`. |
| `INSTANCE_BLOCKED` / `INSTANCE_SUSPENDED` | `getStateInstance` returned `blocked` / `suspended`. |
| `UNKNOWN_INSTANCE_STATE` | Unrecognized `stateInstance`. |
| `GREEN_API_ERROR` | Unusable API response (e.g. `checkAccount` `status=false` or missing `exist`). |
| `SEND_MESSAGE_REJECTED` | `sendMessage` returned an explicit error response. |
| `INTERNAL_ERROR` | Unexpected adapter error before any send. |

---

## 4. RabbitMQ integration

| Component | Value |
|---|---|
| Send exchange | `tasks.messages.direct` (direct) — declared by Go |
| Send routing key | `account.<tenant_account_id>` |
| Send queue (consumed) | `tasks.messages.send.account.<tenant_account_id>` |
| Dead-letter queue (account) | `tasks.messages.send.account.<tenant_account_id>.dead` |
| Admin-notify queue (consumed) | `tasks.messages.tenant_admin_notify.account.<tenant_account_id>` |
| Results queue (published) | `tasks.messages.results_replies_queue` (default exchange) |

Queue parameters match the Go declaration: `durable=true`, `exclusive=false`,
`auto_delete=false`. Collection uses `prefetch_count=1` and manual ack.

### Ack ordering

1. Receive the delivery.
2. Parse and process the task.
3. Form the `TargetResult`.
4. Publish it to the results queue and **wait for the broker confirm**
   (publisher confirms).
5. Only then `basic.ack` the original delivery.

If publishing the result temporarily fails, only the *publish* is retried; the
message is never sent a second time within the same delivery, the result is not
lost, and the original delivery is not acked.

### Dead-letters

A message whose JSON or required UUID identifiers cannot be parsed is published
verbatim to the account dead-letter queue with the header
`x-aba-dead-letter-reason`, confirmed, and only then acked. This prevents an
endless redelivery loop.

### Known at-least-once window

There is no idempotency store yet. If the process dies **after** a successful
`SendMessage` but **before** the result is confirmed, RabbitMQ may redeliver the
task and a duplicate message can be sent. This first release does not promise
exactly-once delivery.

---

## 5. Incoming messages (replies) and statuses

The adapter long-polls GREEN-API `ReceiveNotification` (with `DeleteNotification`
after each batch) and forwards what it finds to
`tasks.messages.results_replies_queue`, exactly like the Playwright adapter's
WebSocket forwarder did.

| GREEN-API notification | Forwarded result |
|---|---|
| `incomingMessageReceived`, personal chat, `textMessage` | `status=replied` + `reply_text`, with `chat_id` (no target/campaign/phone) |
| `outgoingMessageStatus` `delivered` | `status=delivered` |
| `outgoingMessageStatus` `read` | `status=viewed` |

* Only personal (`chatType=user`) text messages are forwarded; groups/channels,
  non-text messages, the adapter's own admin-notification echo (text starting
  with `🔔 Получены новые сообщения:`), and other statuses are ignored.
* The result carries `tenant_account_id`/`account_id` = this process's account
  and an empty `target_id`/`campaign_id`/`phone_number`; the Go
  `ResultConsumer` resolves the target from `chat_id` via
  `chat_phone_mappings`, records the reply, and (after a 45–75 s delay)
  publishes a tenant-admin notification to the account's notify queue.

`ReceiveNotification` only returns queued notifications when the instance has
**no** `webhookUrl` configured **and** the relevant webhook flags are enabled
(GREEN-API disables every notification flag by default on instance creation).
Required for the adapter:

| Setting | Value | Controls |
|---|---|---|
| `webhookUrl` | empty | HTTP-API mode (`ReceiveNotification` works) |
| `incomingWebhook` | `yes` | `incomingMessageReceived` (user replies) |
| `outgoingWebhook` | `yes` | `outgoingMessageStatus` (delivered/read) |
| `outgoingAPIMessageWebhook` | `yes` | notifications for API-sent messages |

`python main.py --check-api` prints the current notification settings and warns
about disabled flags; `python main.py --enable-incoming` enables the three
flags once via `SetSettings` (instance restart, applies within ~5 minutes;
replies sent before enabling were never queued).

## 6. Tenant-admin notifications

The adapter consumes `tasks.messages.tenant_admin_notify.account.<id>`,
renders the notification text (the same layout as the Playwright adapter),
sends it via `SendMessage` (by saved `chat_id` when provided, otherwise after
`CheckAccount`), and publishes a `sent` result with empty
`target_id`/`campaign_id` so the Go orchestrator stores the admin `chat_id` in
`admin_chat_phone_mappings` for this account.

Account isolation is enforced: a notification whose `tenant_account_id` is
missing or differs from this process is logged and dropped (never sent).

### Stale admin chat ids

`admin_chat_phone_mappings` rows written by the **old Playwright adapter** hold
chat ids from a different id space; GREEN-API rejects them
(`SEND_MESSAGE_REJECTED`). Because an explicit rejection proves nothing was
sent, the adapter then resolves the admin chat by phone via `CheckAccount` and
retries **once**, reporting the corrected `chat_id` back — the orchestrator
upserts `admin_chat_phone_mappings`, so the mapping self-heals after the first
successful notification. Unknown outcomes (`DELIVERY_UNKNOWN`) are never
retried.

## 7. GREEN-API usage

| Method | Endpoint | Notes |
|---|---|---|
| `GetStateInstance` | `GET /waInstance{id}/getStateInstance/{token}` | Send is allowed only for `stateInstance=authorized`. |
| `CheckAccount` | `POST /waInstance{id}/checkAccount/{token}` | Body `{"phoneNumber": <integer>}`, no `force`. |
| `SendMessage` | `POST /waInstance{id}/sendMessage/{token}` | Body `{"chatId": ..., "message": ...}`. |

### Phone number format

The orchestrator and GREEN-API use **different** formats, so the adapter
converts between them:

| Format | Example | Used by |
|---|---|---|
| 10 digits starting with `9` | `9601701985` | Go orchestrator task `phone` |
| 11 digits starting with `7`, or 12 starting with `375` | `79601701985` / `375291234567` | GREEN-API `checkAccount` `phoneNumber` |

The adapter maps `9XXXXXXXXX` → `7` + `9XXXXXXXXX` (and passes already
international `7…` / `375…` numbers through) before calling `CheckAccount`.
Any other shape is rejected with `INVALID_PHONE` before any GREEN-API call.

> The result `phone_number` always keeps the **original orchestrator value**
> (`9XXXXXXXXX`), so the Go chat-phone mapping (`phone_normalized`) keeps
> matching.

* `CheckAccount` responses: `exist=true` + non-empty `chatId` → send;
  `exist=false` → `user_not_found_by_phone`; missing `exist` or `status=false`
  → API error (never reported as "user not found").
* GREEN-API `469` ("User get contact info limit reached") starts a 2-hour
  `CheckAccount` cooldown for the process; further cold tasks fail with
  `CHECK_ACCOUNT_RATE_LIMITED` without calling the API.
* `SendMessage` text limit is 4000 characters. `delivered`/`viewed` are **not**
  derived from a `SendMessage` response.
* No automatic retry of `SendMessage` (a timeout is `delivery_unknown`).
* Control methods (`SetSettings`, `Reboot`, …) are never called automatically.

### Old chat ids

A `chat_id` from the old Playwright adapter is passed to `SendMessage`
**verbatim** when `use_chat_id=true`, with no `CheckAccount` and no
transformation. Its compatibility with GREEN-API is not yet confirmed, so the
first real end-to-end test must use a **cold** contact (`use_chat_id=false`) to
exercise `CheckAccount → SendMessage`.

---

## 8. Not implemented in this release

* Attachment delivery (both directions).
* `provider_message_id` in the Go result and any status-correlation migrations.
* Multi-account operation in a single process (one process = one
  `tenant_account_id` + one GREEN-API instance).
* Exactly-once send semantics.
* The shared `tasks.messages.poll_replies` polling mechanism (replies come from
  `ReceiveNotification` instead).

`scripts/green_api_smoke_test.py` is kept for manual experimentation only; it is
**not** a production worker. Real sending happens exclusively through the
RabbitMQ worker.
