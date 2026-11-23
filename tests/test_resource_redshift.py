"""Redshift provisioned clusters: what is and is not a cluster Clamper can pause."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import REGION

from clamper.core.cost import redshift_price
from clamper.core.ledger import STATUS_CLAMPED, STATUS_RELEASED, Entry

ACTION = "redshift-cluster"


def make_cluster(
    redshift: Any,
    identifier: str = "warehouse",
    *,
    node_type: str = "ra3.xlplus",
    nodes: int = 2,
) -> str:
    redshift.create_cluster(
        ClusterIdentifier=identifier,
        NodeType=node_type,
        MasterUsername="admin",
        MasterUserPassword="NotARealPassw0rd",
        ClusterType="multi-node" if nodes > 1 else "single-node",
        NumberOfNodes=nodes,
        Tags=[{"Key": "clamper:managed", "Value": "true"}],
    )
    return identifier


def raw(**overrides: Any) -> dict[str, Any]:
    cluster = {
        "ClusterIdentifier": "warehouse",
        "NodeType": "ra3.xlplus",
        "NumberOfNodes": 2,
        "ClusterStatus": "available",
        "Tags": [{"Key": "clamper:managed", "Value": "true"}],
    }
    cluster.update(overrides)
    return cluster


def test_an_available_cluster_is_a_candidate(clients_moto: Any, moto_redshift: Any) -> None:
    make_cluster(moto_redshift)
    candidate = discover_one(ACTION, clients_moto, REGION, "warehouse")
    assert candidate.clampable is True
    assert candidate.state == {"status": "available"}
    assert candidate.tags["clamper:managed"] == "true"
    assert candidate.description == "2x ra3.xlplus, available"


def test_the_price_is_every_node(clients_moto: Any, moto_redshift: Any) -> None:
    make_cluster(moto_redshift, nodes=4)
    candidate = discover_one(ACTION, clients_moto, REGION, "warehouse")
    assert candidate.price == redshift_price("ra3.xlplus", 4)


def test_an_unlisted_node_type_gets_no_price() -> None:
    candidate = handler(ACTION)._candidate(raw(NodeType="ra9.huge"), REGION)  # type: ignore[attr-defined]
    assert candidate.clampable is True
    assert candidate.price is None


@pytest.mark.parametrize("status", ["paused", "pausing"])
def test_an_already_paused_cluster_is_reported(status: str) -> None:
    candidate = handler(ACTION)._candidate(raw(ClusterStatus=status), REGION)  # type: ignore[attr-defined]
    assert candidate.clampable is False
    assert candidate.reason == f"already {status}"


@pytest.mark.parametrize("status", ["resizing", "modifying", "resuming", "creating"])
def test_a_cluster_mid_change_is_reported_not_clamped(status: str) -> None:
    candidate = handler(ACTION)._candidate(raw(ClusterStatus=status), REGION)  # type: ignore[attr-defined]
    assert candidate.clampable is False
    assert f"status is {status}" in candidate.reason


def test_a_cluster_with_no_identifier_is_skipped() -> None:
    assert handler(ACTION)._candidate(raw(ClusterIdentifier=""), REGION) is None  # type: ignore[attr-defined]


def test_clamp_and_release_round_trip(clients_moto: Any, moto_redshift: Any) -> None:
    make_cluster(moto_redshift)
    trip = round_trip(ACTION, clients_moto, REGION, "warehouse")

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"status": "paused"}
    assert trip.clamped.applied_state == trip.state_after_clamp
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"status": "available"}


def test_a_dry_run_pauses_nothing(clients_moto: Any, moto_redshift: Any) -> None:
    make_cluster(moto_redshift)
    trip = round_trip(ACTION, clients_moto, REGION, "warehouse", dry_run=True)
    assert trip.state_after_clamp == {"status": "available"}


def test_live_state_is_none_for_a_cluster_that_is_gone(clients_moto: Any) -> None:
    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, "never-existed", REGION),
        status=STATUS_CLAMPED,
        previous_state={"status": "available"},
        applied_state={"status": "paused"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    assert handler(ACTION).live_state(clients_moto, entry) is None
