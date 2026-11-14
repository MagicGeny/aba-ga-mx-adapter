"""Queue names and routing keys shared with the Go orchestrator.

These names mirror the Go orchestrator's ``internal/worker/outbox.go`` and
``cmd/orchestrator/main.go``. They must not drift: the adapter and the Go
orchestrator have to agree on the exact queue names.
"""

RESULTS_QUEUE = "tasks.messages.results_replies_queue"

SEND_QUEUE_PREFIX = "tasks.messages.send.account."
DEAD_LETTER_QUEUE_SUFFIX = ".dead"

# Per-account tenant-admin notification queue (result of an incoming reply).
# The Go orchestrator publishes here over the default exchange and only the
# owning account's worker consumes it.
NOTIFY_QUEUE_PREFIX = "tasks.messages.tenant_admin_notify.account."

SEND_EXCHANGE = "tasks.messages.direct"


def account_send_queue(tenant_account_id: str) -> str:
    """Account-specific send queue: tasks.messages.send.account.<uuid>."""
    return SEND_QUEUE_PREFIX + tenant_account_id


def account_send_dead_letter_queue(tenant_account_id: str) -> str:
    """Account-specific dead-letter queue for unparsable tasks."""
    return SEND_QUEUE_PREFIX + tenant_account_id + DEAD_LETTER_QUEUE_SUFFIX


def account_notify_queue(tenant_account_id: str) -> str:
    """Account-specific tenant-admin notification queue."""
    return NOTIFY_QUEUE_PREFIX + tenant_account_id


def account_routing_key(tenant_account_id: str) -> str:
    """Direct-exchange routing key used by the Go orchestrator: account.<uuid>."""
    return "account." + tenant_account_id
