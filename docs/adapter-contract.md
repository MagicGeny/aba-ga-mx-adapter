# Aba-ga-mx-adapter ↔ Orchestrator Contract

This document describes the proposed technical contract between the future
`aba-ga-mx-adapter` (Python) and the existing Go orchestrator.

The scope of this stage is **contract + smoke test only**. RabbitMQ consumers,
the long-running adapter worker, and changes to the Go orchestrator are **not
implemented** in this stage. Everything RabbitMQ-related below is documentation
for the future implementation.

---

## 1. Outgoing Task Contract

Tasks are published by the Go orchestrator to the adapter. Each adapter
process will eventually be configured for one `tenant_account_id` and one
GREEN-API instance. The following fields are **already used** by the existing
Go orchestrator and must be understood by the adapter:

| Field                  | Type    | Description |
|------------------------|---------|-------------|
| `task_id`              | string  | Unique task identifier (UUID). |
| `campaign_id`          | string  | Campaign identifier (UUID). |
| `tenant_id`            | string  | Tenant identifier. |
| `tenant_account_id`    | string  | The sender-account identity (UUID from `tenant_accounts.id`). Also used to scope the per-account RabbitMQ queue and routing key. |
| `account_id`           | string  | GREEN-API instance id (`idInstance`). |
| `messenger`            | string  | Messenger family, e.g. `"MAX"`. |
| `messenger_type`       | string  | Messenger sub-type, e.g. `"v3"`. |
| `phone`                | string  | Recipient phone number as digits only (no `+`, spaces, or separators). GREEN-API MAX `CheckAccount` accepts only 11 digits beginning with `7` (Russian numbers) or 12 digits beginning with `375` (Belarusian numbers), e.g. `79991234567` or `375291234567`. |
| `message_text`         | string  | Text content of the message to send. |
| `use_chat_id`          | bool    | If `true` and a valid `chat_id` is provided, send directly to that chat. |
| `chat_id`              | string  | GREEN-API chat identifier to send to directly (used only when `use_chat_id=true`). |
| `contact_type`         | string  | Type of contact for the recipient (e.g. `"client"`, `"admin"`). |

### Attachment-related fields

The following fields exist on outgoing tasks for attachment delivery:

| Field                  | Type    | Description |
|------------------------|---------|-------------|
| `attachments`          | array   | List of attachment descriptors. |
| `attachment.type`      | string  | Attachment type (image, document, etc.). |
| `attachment.url`       | string  | Public URL of the attachment. |

> ⚠️ **Out of scope for this stage.** Attachment delivery is not implemented.
> The smoke test covers text messages only.

### Expected Future Behavior

- If `use_chat_id=true` and a valid `chat_id` is provided, the adapter MAY send
  directly to that chat without calling `CheckAccount`.
- Otherwise, the adapter MUST resolve the recipient using `CheckAccount` with
  the task's `phone` field.
- Before calling `CheckAccount`, the adapter MUST validate `phone` as digits
  only: 11 digits beginning with `7` (Russian numbers) or 12 digits beginning
  with `375` (Belarusian numbers). Arbitrary 7–15 digit numbers are NOT valid
  MAX `CheckAccount` inputs.
- **The adapter MUST NOT send a message when `CheckAccount` returns
  `exist=false`** (the recipient has no MAX account on this instance).
- `CheckAccount` MUST NOT use `force=true` by default — the default caching
  behavior is sufficient (GREEN-API caches `ChatExist` results for 2 hours).

---

## 2. Result Contract

After processing a task (or when a notification is received), the adapter
publishes a result to the results queue. These fields are **already consumed**
by the existing Go orchestrator:

| Field                  | Type    | Description |
|------------------------|---------|-------------|
| `target_id`            | string  | Target identifier within the campaign. |
| `campaign_id`          | string  | Campaign identifier (UUID). |
| `tenant_id`            | string  | Tenant identifier. |
| `tenant_account_id`    | string  | Sender-account identity (UUID). |
| `account_id`           | string  | GREEN-API instance id (`idInstance`). |
| `phone_number`         | string  | Recipient phone number. |
| `status`               | string  | Delivery status. See status mapping below. |
| `reply_text`           | string  | Content of an inbound reply (if applicable). |
| `error_code`           | string  | Error code, if the operation failed. |
| `error_message`        | string  | Human-readable error message. |
| `timestamp`            | string  | ISO-8601 timestamp of the result. |
| `chat_id`              | string  | GREEN-API chat identifier of the recipient. |
| `messenger_type`       | string  | Messenger sub-type, e.g. `"v3"`. |

### Proposed Extension: `provider_message_id`

| Field                  | Type    | Description |
|------------------------|---------|-------------|
| `provider_message_id`  | string  | GREEN-API `idMessage` for the outgoing message. |

> ⚠️ **Proposed extension.** Adding `provider_message_id` to the result
> contract **may require a change to the Go orchestrator** to persist and
> correlate this field. This field is NOT implemented in this stage. Do not
> modify the orchestrator now.

### Status Mapping

| GREEN-API event / status          | Orchestrator `status` | Notes |
|-----------------------------------|-----------------------|-------|
| `SendMessage` API response success | *(accepted — not yet delivered)* | The request was accepted by GREEN-API. Do NOT report as `delivered` or `viewed`. |
| OutgoingMessageStatus `delivered`  | `delivered`            | The message was delivered to the recipient's MAX app. |
| OutgoingMessageStatus `read`       | `viewed`               | The recipient opened/read the message. |
| Incoming customer message          | `replied`              | *(Future — not implemented in this stage.)* |
| OutgoingMessageStatus `failed`     | `failed`               | Server-side send error. |
| OutgoingMessageStatus `noAccount`  | `failed`               | Recipient has no MAX account. |
| OutgoingMessageStatus `notInGroup` | `failed`               | Sender not in group chat. |

---

## 3. RabbitMQ Integration Contract

> ⚠️ **Documentation only.** RabbitMQ is not implemented in this stage.

### Exchange & Routing

| Component              | Value |
|------------------------|-------|
| Direct exchange        | `tasks.messages.direct` |
| Send routing key       | `account.<tenant_account_id>` |
| Send queue name        | `tasks.messages.send.account.<tenant_account_id>` |
| Admin notification queue | `tasks.messages.tenant_admin_notify.account.<tenant_account_id>` |
| Results queue          | `tasks.messages.results_replies_queue` |

### Future Adapter Configuration

Each adapter process will eventually be configured for:

- One `tenant_account_id` — determines which send queue and routing key to
  consume from.
- One GREEN-API instance — determined by `account_id` (`idInstance`),
  `GREEN_API_ID_INSTANCE`, and `GREEN_API_TOKEN_INSTANCE`.

### Notes

- The results queue (`tasks.messages.results_replies_queue`) is currently a
  shared, non-vendored queue. A future stage may vendor it per account, the
  same way the send and admin-notify queues are vendored today.
- The legacy shared admin-notification queue
  (`tasks.messages.tenant_admin_notify`) is documented for context but is no
  longer used; per-account isolation uses
  `tasks.messages.tenant_admin_notify.account.<tenant_account_id>`.