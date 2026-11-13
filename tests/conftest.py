"""Shared fixtures.

No test in this suite touches a real AWS account. Two mocking strategies,
chosen per test rather than globally:

- **botocore's `Stubber`** validates every request against the real service
  model, so a wrong parameter name fails in CI rather than in somebody's
  account. It holds no state.
- **moto** holds state, which the ledger round-trips need: a write-ahead
  ledger is only interesting if you can read back what it wrote.

The credential environment variables below exist so a developer's real
`AWS_PROFILE` can never be picked up by a test. A suite that can reach a
real account is a suite that can clamp a real resource.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pytest
from support import ACCOUNT, REGION

#: A counter, so each test gets its own table inside one shared mock.
#:
#: Entering and leaving moto's `mock_aws` per test costs several seconds on
#: a filesystem where botocore has to re-read its service models, and this
#: suite has enough ledger tests for that to dominate the run. One mock for
#: the session plus a fresh table per test gives the same isolation for a
#: fraction of the time.
_TABLE_NUMBERS = itertools.count(1)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for a test to reach a real account.

    `autouse` and unconditional. `AWS_PROFILE` in particular is the one that
    would otherwise leak in from a developer's shell, and the consequence
    here is not a failing test — it is a stopped instance.
    """
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    # moto refuses to run against a real endpoint if this is set to a real
    # account; the documentation placeholder is the right value.
    monkeypatch.setenv("MOTO_ACCOUNT_ID", ACCOUNT)


@pytest.fixture()
def clients() -> Any:
    """A real `Clients` over a session with no usable credentials.

    Used by tests that pre-seed it with a stubbed client. Deliberately the
    real class: it is the only client factory in the package and replacing
    it would mean the dispatch under test was never exercised.
    """
    import boto3

    from clamper.core.session import Clients

    return Clients(boto3.Session(region_name=REGION), default_region=REGION)


@pytest.fixture()
def dynamodb_table(moto_dynamodb: Any) -> str:
    """The ledger table, shaped exactly as the stack creates it.

    Including the GSI, because `active()` and `history()` query it and a
    test against a table without it would pass while the real thing failed.
    """
    table = f"clamper-ledger-{next(_TABLE_NUMBERS)}"
    moto_dynamodb.create_table(
        TableName=table,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by-status",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return table


@pytest.fixture(scope="session")
def moto_dynamodb() -> Iterator[Any]:
    """One moto mock and one DynamoDB client for the whole session."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.client(
            "dynamodb",
            region_name=REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )


@pytest.fixture()
def ledger(dynamodb_table: str, moto_dynamodb: Any) -> Any:
    """A real `Ledger` against moto, through the real `Clients`."""
    import boto3

    from clamper.core.ledger import Ledger
    from clamper.core.session import Clients

    clients = Clients(boto3.Session(region_name=REGION), default_region=REGION)
    # The session-scoped client, rather than a second one: building a
    # DynamoDB client is the slow part, and `stub` is how a caller hands one
    # in.
    clients.stub("dynamodb", moto_dynamodb, region=REGION)
    return Ledger(clients, dynamodb_table)


@pytest.fixture()
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> str:
    """Freeze the ledger's clock, so timestamps are assertable.

    Only the ledger's `_now` is frozen, not `datetime` globally — a global
    freeze breaks botocore's request signing.
    """
    from clamper.core import ledger as ledger_module

    stamp = "2026-11-13T15:30:00+00:00"
    monkeypatch.setattr(ledger_module, "_now", lambda: stamp)
    return stamp
