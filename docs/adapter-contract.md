# Adapter Contract: GREEN-API MAX (`aba-ga-mx-adapter`)

This document defines how a future `aba-ga-mx-adapter` service talks to
**MAX** through the **GREEN-API HTTP API**.

Scope: outbound text messaging only. No chatbot, no webhooks, no inbound
processing.

## 1. Transport and credentials

The adapter uses plain HTTPS calls to GREEN-API. No socket, no browser,
no persistent session.

Configuration comes from the environment:

| Variable | Required | Description |
|---|---|---|
| `GREEN_API_BASE_URL` | yes | GREEN-API base URL, e.g. `https://7107.api.greenapi.com` |
| `GREEN_API_ID_INSTANCE` | yes | Instance numeric ID (digits only) |
| `GREEN_API_TOKEN_INSTANCE` | yes | Instance API token |
| `TEST_PHONE_NUMBER` | for smoke test | Recipient phone, digits only (see §3) |
| `TEST_MESSAGE` | for smoke test | Test message text |
| `SMOKE_TEST_TIMEOUT_SECONDS` | no | Wait timeout for the smoke test, default `180` |

### 1.1 URL pattern

All method calls use the GREEN-API v3 `waInstance` URL shape:

```text
{BASE_URL}/waInstance{ID_INSTANCE}/{METHOD}/{TOKEN}
```

Examples:

```text
{base}/waInstance1234567/checkAccount/token_abc
{base}/waInstance1234567/sendMessage/token_abc
{base}/waInstance1234567/receiveNotification/token_abc
{base}/waInstance1234567/deleteNotification/token_abc/8888888   # receiptId appended
```

The API token is part of the URL path. Clients **must never** log full
request URLs. Log only the method name (e.g. `checkAccount`) or a masked
URL.

## 2. Pre-flight: notification settings

Before the adapter (or the smoke test) can observe message statuses, the
instance must be configured:

- `webhookUrl` — **empty**. With HTTP-API polling (`receiveNotification`),
  no custom webhook URL may be set; otherwise the server skips queueing
  notifications for polling.
- Outgoing status notifications — **enabled**:
  - `outgoingMessageWebhook`
  - `outgoingAPIMessageWebhook`
  - `outgoingWebhook`

Either the console or the `SetSettings` method may be used to set these.
Without them the adapter will never receive `outgoingMessageStatus`
notifications.

Use a **dedicated test instance with an empty notification queue** for the
smoke test. Stray queue items abort the run by design.

## 3. Addressing and validation

GREEN-API MAX addresses a recipient by `chatId`, derived from the phone
number — often identical to the digits themselves.

Client-side rules (server enforces the rest and returns `400` on
mismatch):

- `TEST_PHONE_NUMBER` (and any recipient phone) must be **7–15 digits**,
  no `+`, no spaces, no dashes (`^\d{7,15}$`).
- GREEN-API MAX in particular accepts only 11–12 digit numbers with the
  `7` (Russia) or `375` (Belarus) prefixes. The server is authoritative
  here, not the client.

## 4. Sending flow

The canonical send sequence is:

```text
CheckAccount -> SendMessage -> ReceiveNotification -> DeleteNotification
```

### 4.1 CheckAccount

`POST {base}/waInstance{id}/checkAccount/{token}`

Request body (`phoneNumber` is an **integer** per GREEN-API docs):

```json
{ "phoneNumber": 79991234567 }
```

Optional `force: true` bypasses the ~2-hour cache. Default `force` is
`false` — the adapter uses the cached value.

Success responses:

```json
{ "exist": true, "chatId": "79991234567", "fromCache": false }
{ "exist": true, "chatId": "79991234567", "fromCache": true }
{ "exist": false, "chatId": "", "fromCache": false }
```

Rules:

- Proceed to `SendMessage` **only** when `exist == true` **and**
  `chatId` is a non-empty string.
- Sometimes GREEN-API answers with `{"status": false, "reason": "..."}`
  (instance starting, rate limit). That is **not** proof of absence —
  treat it as an error and stop.

### 4.2 SendMessage

`POST {base}/waInstance{id}/sendMessage/{token}`

Request body:

```json
{ "chatId": "79991234567", "message": "...text, up to 4000 chars, UTF-8..." }
```

Success response:

```json
{ "idMessage": "BAE5F7B9E1234567" }
```

- A `200` + `idMessage` means the message was **accepted** — not
  delivered, not read.
- Never retry automatically after a send timeout: the server may have
  accepted the request even if the response was lost. Check the
  recipient device first.

Error codes to expect: `400` (validation/length), `403` (suspended),
`404` (instance not found), `409` (registration/synchronization),
`422` (invalid recipient), `429`/`469` (rate limit), `500+` (server).

### 4.3 ReceiveNotification (HTTP-API polling)

`GET {base}/waInstance{id}/receiveNotification/{token}?receiveTimeout=N`

- `receiveTimeout`: long-poll wait, **5–60 s** (server clamps shorter
  values down to 5).

Each notification arrives as an envelope:

```json
{
  "receiptId": 1234567,
  "body": {
    "typeWebhook": "outgoingMessageStatus",
    "chatId": "79991234567",
    "instanceData": {"idInstance": 3100000000, "wid": "...", "typeInstance": "..."},
    "timestamp": 1755591519,
    "idMessage": "BAE5F7B9E1234567",
    "status": "read",
    "description": "..."
  }
}
```

- An empty/timeout response is `null` or `{}` — keep polling.
- Only notifications with `body.typeWebhook == "outgoingMessageStatus"`
  and `body.idMessage` equal to the sent message's `idMessage` belong to
  this send. Anything else is **unrelated**: report it, do **not**
  delete it, stop.

### 4.4 OutgoingMessageStatus values

`body.status` is one of (per GREEN-API docs):

| Status | Meaning | Adapter action |
|---|---|---|
| `delivered` | Reached the device | Log, keep waiting |
| `read` | Opened by recipient | **Success** — finish |
| `failed` | Terminal failure | Stop with error |
| `noAccount` | Recipient has no MAX account | Stop with error |
| `notInGroup` | Bad target | Stop with error |

`read` is the only success state. `delivered` is intermediate. Any other
value is treated as intermediate for now (logged, kept waiting) so a new
server-side value cannot silently mean something it doesn't.

### 4.5 DeleteNotification

`DELETE {base}/waInstance{id}/deleteNotification/{token}/{receiptId}`

Response:

```json
{ "result": true }
```

Rules:

- Delete **only** notifications already classified as related to the
  current send, identified by `receiptId`.
- Never delete unrelated items.
- If deletion reports `result: false`, log the reason and continue —
  a stale queue item is a warning, not a success-blocker.

## 5. Happy path (end-to-end)

1. `CheckAccount` for the recipient phone; require `exist == true` and a
   valid `chatId`.
2. Operator confirms by typing `SEND` (interactive only — no
   unattended sending).
3. `SendMessage` to the `chatId`; capture `idMessage`.
4. Poll `ReceiveNotification`, deleting and classifying each related
   notification, until `status == "read"` for that `idMessage`
   (or timeout / terminal status → failure).

## 6. What is out of scope

- Chatbots / inbound message processing.
- Webhook receiver mode (`webhookUrl` stays empty in this design).
- Attachments, media, buttons, polls, location, contacts.
- Queue-based multi-tenant plumbing (RabbitMQ topics, result queues).
- Retry-with-backoff policy beyond "don't auto-retry a timed-out send".

## 7. Reference links (official GREEN-API v3)

- Console: https://console.green-api.com
- CheckAccount: https://green-api.com/v3/docs/api/service/CheckAccount/
- SendMessage: https://green-api.com/v3/docs/api/sending/SendMessage/
- Notification format: https://green-api.com/v3/docs/api/receiving/notifications-format/
- ReceiveNotification: https://green-api.com/v3/docs/api/receiving/technology-http-api/ReceiveNotification/
- DeleteNotification: https://green-api.com/v3/docs/api/receiving/technology-http-api/DeleteNotification/
- OutgoingMessageStatus: https://green-api.com/v3/docs/api/receiving/notifications-format/statuses/OutgoingMessageStatus/
- HTTP status codes: https://green-api.com/v3/docs/api/GreenAPI_ERRORS/
- Instance settings: https://green-api.com/v3/docs/api/account/
- Phone validation rules: https://green-api.com/v3/docs/api/sending/SendMessage/#phone


