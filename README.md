# aba-ga-mx-adapter (GREEN-API MAX)

Outbound text messaging to **MAX** via the **GREEN-API HTTP API**.

## What is here

| File | Purpose |
|---|---|
| `docs/adapter-contract.md` | Adapter contract: URLs, payloads, statuses, links |
| `scripts/green_api_smoke_test.py` | 4-stage smoke test (CheckAccount → SendMessage → ReceiveNotification → read) |
| `tests/test_green_api_smoke_test.py` | Local mocked tests (respx, no network) |
| `.env.example` | Credentials template |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your credentials
```

## Smoke test

**1. Prepare the instance** (console or `SetSettings`):

- `webhookUrl` — empty
- `outgoingMessageWebhook`, `outgoingAPIMessageWebhook`,
  `outgoingWebhook` — enabled

Use a dedicated test instance with an empty notification queue.

**2. Check-only mode** — verifies the recipient is on MAX, sends nothing:

```bash
python scripts/green_api_smoke_test.py --check-only
```

**3. Full run** — sends one message and waits for the `read` status:

```bash
python scripts/green_api_smoke_test.py --send-and-wait-read
```

The operator must type `SEND` to confirm before anything is sent.

During stage 3, open the delivered message on the recipient's device to
generate the `read` status.

## Unit tests

```bash
python -m pytest tests/ -q
```

All tests are mocked (respx) — no real network, no messages sent.

## Success criteria

- `CheckAccount` returns `exist=true` with a valid `chatId`.
- `SendMessage` returns `idMessage`.
- An `outgoingMessageStatus` notification arrives with a matching
  `idMessage` and `status=read`.

See `docs/adapter-contract.md` for the full contract.
