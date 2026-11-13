"""Shared fixtures.

No test in this suite touches a real AWS account. Two mocking strategies,
chosen per test rather than globally:

- **botocore's `Stubber`** validates every request against the real service
  model, so a wrong parameter name fails in CI rather than in somebody's
  account. It holds no state, which makes it right for the actuator tests.
- **moto** holds state, which the ledger and the round-trip tests need: a
  write-ahead ledger is only interesting if you can read back what it wrote,
  and a clamp/release round trip is only interesting if the state after the
  clamp is a real transition rather than a fixture somebody wrote to match.

**On speed.** Entering and leaving `mock_aws` per test, and building a boto3
client per test, cost several seconds each on a filesystem where botocore has
to re-read its service models. With sixty-odd moto tests that dominated the
run — the ledger file alone took 236 seconds. So there is one mock and one
set of clients for the whole session, and `mock.reset()` between tests gives
the same isolation. Clients stay valid across a reset; only the backends'
state is cleared.

The credential environment variables below exist so a developer's real
`AWS_PROFILE` can never be picked up. In a suite for a tool that stops
infrastructure, a leaked profile is not a flaky test.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from support import ACCOUNT, REGION

#: A counter, so each test gets a table name of its own even within a run.
_TABLE_NUMBERS = itertools.count(1)

#: Services the shared `Clients` will be asked for. Listed so the first test
#: pays for all of them at once rather than one surprise at a time.
SERVICES = (
    "ec2",
    "autoscaling",
    "ecs",
    "rds",
    "lambda",
    "sagemaker",
    "dynamodb",
    "sns",
    "ssm",
)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for a test to reach a real account."""
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
    monkeypatch.setenv("MOTO_ACCOUNT_ID", ACCOUNT)


# --- moto, once per session ---------------------------------------------


@pytest.fixture(scope="session")
def _aws_mock() -> Iterator[Any]:
    from moto import mock_aws

    mock = mock_aws()
    mock.start()
    try:
        yield mock
    finally:
        mock.stop()


@pytest.fixture()
def aws(_aws_mock: Any) -> Any:
    """A clean mocked AWS for one test.

    `reset()` clears every backend's state while leaving the mock running, so
    clients built in an earlier test stay usable. That is what makes the
    session-scoped client cache safe.
    """
    _aws_mock.reset()
    return _aws_mock


@pytest.fixture(scope="session")
def _shared_clients() -> Any:
    """One `Clients` for the session, so service models are parsed once.

    Deliberately the real class rather than a fake: it is the only client
    factory in the package, and replacing it would mean the dispatch under
    test was never exercised.
    """
    import boto3

    from clamper.core.session import Clients

    return Clients(boto3.Session(region_name=REGION), default_region=REGION)


@pytest.fixture()
def clients_moto(aws: Any, _shared_clients: Any) -> Any:
    """The shared `Clients`, against a freshly-reset moto."""
    return _shared_clients


@pytest.fixture()
def aws_client(clients_moto: Any) -> Callable[[str], Any]:
    """A boto3 client for a service, for a test to set the world up with."""

    def get(service: str) -> Any:
        return clients_moto.get(service, region=REGION)

    return get


@pytest.fixture()
def moto_ec2(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("ec2")


@pytest.fixture()
def moto_autoscaling(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("autoscaling")


@pytest.fixture()
def moto_ecs(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("ecs")


@pytest.fixture()
def moto_rds(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("rds")


@pytest.fixture()
def moto_lambda(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("lambda")


@pytest.fixture()
def moto_sagemaker(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("sagemaker")


@pytest.fixture()
def moto_dynamodb(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("dynamodb")


@pytest.fixture()
def moto_sns(aws_client: Callable[[str], Any]) -> Any:
    return aws_client("sns")


# --- the ledger ---------------------------------------------------------


@pytest.fixture()
def dynamodb_table(moto_dynamodb: Any) -> str:
    """The ledger table, shaped exactly as the stack creates it.

    Including the GSI, because `active()` and `history()` query it and a test
    against a table without it would pass while the real thing failed.
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


@pytest.fixture()
def ledger(clients_moto: Any, dynamodb_table: str) -> Any:
    """A real `Ledger` against moto, through the real `Clients`."""
    from clamper.core.ledger import Ledger

    return Ledger(clients_moto, dynamodb_table)


@pytest.fixture()
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> str:
    """Freeze the ledger's clock, so timestamps are assertable.

    Only the ledger's `_now`, not `datetime` globally — a global freeze
    breaks botocore's request signing.
    """
    from clamper.core import ledger as ledger_module

    stamp = "2026-11-13T15:30:00+00:00"
    monkeypatch.setattr(ledger_module, "_now", lambda: stamp)
    return stamp
