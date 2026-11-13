"""Aurora clusters: what is and is not a cluster Clamper can stop."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import REGION

from clamper.core.actions import load_registry
from clamper.core.ledger import STATUS_CLAMPED, STATUS_RELEASED, Entry

ACTION = "aurora-cluster"


def make_cluster(
    rds: Any, identifier: str = "reports", *, engine: str = "aurora-postgresql"
) -> str:
    rds.create_db_cluster(
        DBClusterIdentifier=identifier,
        Engine=engine,
        MasterUsername="admin",
        MasterUserPassword="not-a-real-password",
        Tags=[{"Key": "clamper:managed", "Value": "true"}],
    )
    return identifier


def raw(**overrides: Any) -> dict[str, Any]:
    cluster = {
        "DBClusterIdentifier": "reports",
        "Engine": "aurora-postgresql",
        "EngineMode": "provisioned",
        "Status": "available",
        "DBClusterMembers": [{"DBInstanceIdentifier": "reports-1"}],
    }
    cluster.update(overrides)
    return cluster


def test_the_action_declares_the_seven_day_restart() -> None:
    assert load_registry().get(ACTION).restart_after_days == 7


def test_an_available_cluster_is_a_candidate(clients_moto: Any, moto_rds: Any) -> None:
    make_cluster(moto_rds)
    candidate = discover_one(ACTION, clients_moto, REGION, "reports")
    assert candidate.clampable is True
    assert candidate.state == {"status": "available"}
    assert candidate.tags["clamper:managed"] == "true"


def test_no_price_is_claimed_for_a_cluster(clients_moto: Any, moto_rds: Any) -> None:
    """The cost is the members', which DescribeDBClusters names but does not
    size. No figure rather than a guessed one."""
    make_cluster(moto_rds)
    assert discover_one(ACTION, clients_moto, REGION, "reports").price is None


def test_a_non_aurora_cluster_is_not_this_action() -> None:
    assert handler(ACTION)._candidate(raw(Engine="mysql"), REGION) is None  # type: ignore[attr-defined]


def test_serverless_v1_is_reported_not_clamped() -> None:
    candidate = handler(ACTION)._candidate(raw(EngineMode="serverless"), REGION)  # type: ignore[attr-defined]
    assert candidate.clampable is False
    assert "Serverless v1" in candidate.reason


def test_a_cross_region_replica_is_reported_not_clamped() -> None:
    candidate = handler(ACTION)._candidate(  # type: ignore[attr-defined]
        raw(ReplicationSourceIdentifier="arn:aws:rds:eu-west-2:123456789012:cluster:primary"),
        REGION,
    )
    assert candidate.clampable is False
    assert "read replica" in candidate.reason


@pytest.mark.parametrize("status", ["stopped", "stopping"])
def test_an_already_stopped_cluster_is_reported(status: str) -> None:
    candidate = handler(ACTION)._candidate(raw(Status=status), REGION)  # type: ignore[attr-defined]
    assert candidate.clampable is False
    assert f"already {status}" == candidate.reason


def test_clamp_and_release_round_trip(clients_moto: Any, moto_rds: Any) -> None:
    make_cluster(moto_rds)
    trip = round_trip(ACTION, clients_moto, REGION, "reports")

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"status": "stopped"}
    assert trip.clamped.applied_state == trip.state_after_clamp
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"status": "available"}


def test_live_state_is_none_for_a_cluster_that_is_gone(clients_moto: Any) -> None:
    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, "never-existed", REGION),
        status=STATUS_CLAMPED,
        previous_state={"status": "available"},
        applied_state={"status": "stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    assert handler(ACTION).live_state(clients_moto, entry) is None
