"""Pytest configuration.

Makes both ``scripts/`` (legacy smoke test) and the repository root (the
``aba_ga_mx_adapter`` package) importable, and provides shared fixtures.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for path in (str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest  # noqa: E402

from aba_ga_mx_adapter.config import AdapterConfig  # noqa: E402

TENANT_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def valid_env():
    """A complete, valid environment mapping for AdapterConfig."""
    return {
        "TENANT_ACCOUNT_ID": TENANT_ACCOUNT_ID,
        "RABBITMQ_URL": "amqp://guest:guest@localhost:5672/",
        "GREEN_API_BASE_URL": "https://example.green-api.com",
        "GREEN_API_ID_INSTANCE": "1101000001",
        "GREEN_API_TOKEN_INSTANCE": "test-token-value",
        "HTTP_TIMEOUT_SECONDS": "30",
        "LOG_LEVEL": "INFO",
    }


@pytest.fixture
def config(valid_env):
    return AdapterConfig.from_env(valid_env, load_env_file=False)
