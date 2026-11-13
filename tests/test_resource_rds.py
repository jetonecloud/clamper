"""RDS instances: what AWS refuses, the restart caveat, and the round trip."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import REGION

from clamper.core.actions import load_registry
from clamper.core.ledger import STATUS_CLAMPED, STATUS_PENDING, STATUS_RELEASED, Entry

ACTION = "rds-instance"


def make_instance(
    rds: Any,
    identifier: str = "orders-db",
    *,
    instance_class: str = "db.m5.large",
    engine: str = "postgres",
    multi_az: bool = False,
    tags: dict[str, str] | None = None,
) -> str:
    rds.create_db_instance(
        DBInstanceIdentifier=identifier,
        DBInstanceClass=instance_class,
        Engine=engine,
        AllocatedStorage=20,
        MasterUsername="admin",
        MasterUserPassword="not-a-real-password",
        MultiAZ=multi_az,
        Tags=[{"Key": key, "Value": value} for key, value in (tags or {}).items()],
    )
    return identifier


def entry_for(identifier: str = "orders-db") -> Entry:
    return Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, identifier, REGION),
        status=STATUS_CLAMPED,
        previous_state={"status": "available"},
        applied_state={"status": "stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )


def test_the_action_declares_the_seven_day_restart() -> None:
    """AWS starts a stopped DB instance after seven days. The reconcile run
    re-stops it, and it can only do that if the row says so."""
    action = load_registry().get(ACTION)
    assert action.restarts_itself
    assert action.restart_after_days == 7


# --- discovery ----------------------------------------------------------


def test_an_available_instance_is_a_candidate(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds, tags={"clamper:managed": "true"})
    candidate = discover_one(ACTION, clients_moto, REGION, "orders-db")

    assert candidate.clampable is True
    assert candidate.state == {"status": "available"}
    assert candidate.tags["clamper:managed"] == "true"
    assert "db.m5.large" in candidate.description
    assert candidate.hourly_usd == 0.178


def test_multi_az_doubles_the_instance_hour_figure(clients_moto: Any, moto_rds: Any) -> None:
    """Multi-AZ is two instances, so 2x is exact for the instance-hour
    component, which is the only component a stop saves."""
    make_instance(moto_rds, multi_az=True)
    candidate = discover_one(ACTION, clients_moto, REGION, "orders-db")
    assert candidate.hourly_usd == pytest.approx(0.356)
    assert "Multi-AZ" in candidate.description


def test_a_read_replica_source_is_reported_not_clamped(clients_moto: Any, moto_rds: Any) -> None:
    """AWS: "You can't stop a DB instance that has a read replica, or that is
    a read replica." Both are reported with the reason, not skipped."""
    make_instance(moto_rds)
    moto_rds.create_db_instance_read_replica(
        DBInstanceIdentifier="orders-replica", SourceDBInstanceIdentifier="orders-db"
    )
    source = discover_one(ACTION, clients_moto, REGION, "orders-db")
    replica = discover_one(ACTION, clients_moto, REGION, "orders-replica")

    assert source.clampable is False
    assert "has a read replica" in (source.reason or "")
    assert replica.clampable is False
    assert "is a read replica" in (replica.reason or "")


def test_sql_server_multi_az_is_reported_not_clamped(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds, engine="sqlserver-se", multi_az=True, instance_class="db.m5.xlarge")
    candidate = discover_one(ACTION, clients_moto, REGION, "orders-db")
    assert candidate.clampable is False
    assert "SQL Server" in (candidate.reason or "")


def test_an_aurora_member_points_at_the_cluster(clients_moto: Any) -> None:
    """Built directly rather than through moto, which does not model the
    member relationship faithfully enough to be worth the setup."""
    candidate = handler(ACTION)._candidate(  # type: ignore[attr-defined]
        {
            "DBInstanceIdentifier": "reports-1",
            "DBInstanceClass": "db.r5.large",
            "Engine": "aurora-postgresql",
            "DBInstanceStatus": "available",
            "DBClusterIdentifier": "reports",
        },
        REGION,
    )
    assert candidate.clampable is False
    assert "reports" in (candidate.reason or "")
    assert "aurora-cluster" in (candidate.reason or "")


def test_an_already_stopped_instance_is_reported(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds)
    moto_rds.stop_db_instance(DBInstanceIdentifier="orders-db")
    candidate = discover_one(ACTION, clients_moto, REGION, "orders-db")
    assert candidate.clampable is False
    assert "already stopped" in (candidate.reason or "")


@pytest.mark.parametrize("status", ["creating", "backing-up", "modifying", "rebooting"])
def test_a_transitional_status_is_not_attempted(status: str) -> None:
    candidate = handler(ACTION)._candidate(  # type: ignore[attr-defined]
        {
            "DBInstanceIdentifier": "orders-db",
            "DBInstanceClass": "db.m5.large",
            "Engine": "postgres",
            "DBInstanceStatus": status,
        },
        REGION,
    )
    assert candidate.clampable is False
    assert status in (candidate.reason or "")


# --- the round trip -----------------------------------------------------


def test_clamp_and_release_round_trip(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds)
    trip = round_trip(ACTION, clients_moto, REGION, "orders-db")

    assert trip.recorded.status == STATUS_PENDING
    assert trip.recorded.previous_state == {"status": "available"}
    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"status": "stopped"}
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"status": "available"}


def test_the_applied_state_matches_what_aws_does(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds)
    trip = round_trip(ACTION, clients_moto, REGION, "orders-db")
    assert trip.clamped.applied_state == trip.state_after_clamp


def test_a_dry_run_leaves_the_instance_running(clients_moto: Any, moto_rds: Any) -> None:
    make_instance(moto_rds)
    trip = round_trip(ACTION, clients_moto, REGION, "orders-db", dry_run=True)
    assert trip.state_after_clamp == {"status": "available"}


def test_live_state_is_none_for_an_instance_that_is_gone(clients_moto: Any) -> None:
    """A deleted database is reported, never treated as released."""
    assert handler(ACTION).live_state(clients_moto, entry_for("never-existed")) is None


def test_the_release_names_only_the_recorded_instance() -> None:
    assert handler(ACTION).release_params(entry_for()) == {"DBInstanceIdentifier": "orders-db"}
