"""ECS services: the cluster/service identity, batching, and the round trip."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import REGION

from clamper.core.ledger import STATUS_CLAMPED, STATUS_PENDING, STATUS_RELEASED, Entry
from clamper.core.resources.ecs import split_target

ACTION = "ecs-service"


def make_service(
    ecs: Any,
    *,
    cluster: str = "prod",
    service: str = "api",
    desired: int = 3,
    tags: dict[str, str] | None = None,
) -> str:
    ecs.create_cluster(clusterName=cluster)
    ecs.register_task_definition(
        family=f"{service}-task",
        containerDefinitions=[{"name": "app", "image": "nginx", "memory": 256, "cpu": 128}],
    )
    ecs.create_service(
        cluster=cluster,
        serviceName=service,
        taskDefinition=f"{service}-task",
        desiredCount=desired,
        tags=[{"key": key, "value": value} for key, value in (tags or {}).items()],
    )
    return f"{cluster}/{service}"


def entry_with(previous: dict[str, Any], *, target_id: str = "prod/api") -> Entry:
    return Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, target_id, REGION),
        status=STATUS_CLAMPED,
        previous_state=previous,
        applied_state={"desired_count": 0},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )


# --- the identity -------------------------------------------------------


def test_the_target_id_is_the_cluster_and_the_service() -> None:
    """An ECS service's identity is a pair, so the target id carries both.
    That keeps the ledger entry self-sufficient: a release months later needs
    no rediscovery to know which cluster the service was in."""
    assert split_target("prod/api") == ("prod", "api")


@pytest.mark.parametrize("bad", ["api", "/api", "prod/", "", "prod/api/extra"])
def test_a_malformed_target_id_is_refused(bad: str) -> None:
    """This value reaches a real AWS call and a ledger entry is untrusted
    input, so it is parsed strictly rather than guessed at."""
    with pytest.raises(ValueError):
        split_target(bad)


# --- discovery ----------------------------------------------------------


def test_an_active_service_is_a_candidate(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs, tags={"clamper:managed": "true"})
    candidate = discover_one(ACTION, clients_moto, REGION, "prod/api")

    assert candidate.clampable is True
    assert candidate.state == {"desired_count": 3}
    assert candidate.tags["clamper:managed"] == "true"
    assert "desired 3" in candidate.description


def test_a_service_at_zero_is_reported_not_clamped(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs, desired=0)
    candidate = discover_one(ACTION, clients_moto, REGION, "prod/api")
    assert candidate.clampable is False
    assert "already zero" in (candidate.reason or "")


def test_no_price_is_claimed_for_an_ecs_service(clients_moto: Any, moto_ecs: Any) -> None:
    """The cost depends on the task definition's reservations, the launch
    type, and for EC2 launch type on instances the service does not own. No
    figure rather than a wrong one."""
    make_service(moto_ecs)
    candidate = discover_one(ACTION, clients_moto, REGION, "prod/api")
    assert candidate.price is None


def test_services_are_described_in_batches_of_ten(clients_moto: Any, moto_ecs: Any) -> None:
    """DescribeServices takes ten ARNs at most. A cluster with eleven
    services and no batching would describe ten and silently miss one, which
    presents as a service Clamper never mentioned rather than as an error."""
    moto_ecs.create_cluster(clusterName="big")
    moto_ecs.register_task_definition(
        family="t",
        containerDefinitions=[{"name": "a", "image": "nginx", "memory": 256, "cpu": 128}],
    )
    for index in range(23):
        moto_ecs.create_service(
            cluster="big",
            serviceName=f"svc-{index:02d}",
            taskDefinition="t",
            desiredCount=1,
        )
    found = handler(ACTION).discover(clients_moto, REGION)
    assert len([item for item in found if item.target_id.startswith("big/")]) == 23


def test_services_across_several_clusters_are_all_found(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs, cluster="prod", service="api")
    make_service(moto_ecs, cluster="staging", service="api")
    found = {item.target_id for item in handler(ACTION).discover(clients_moto, REGION)}
    assert {"prod/api", "staging/api"} <= found


# --- the round trip -----------------------------------------------------


def test_clamp_and_release_round_trip(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs, desired=5)
    trip = round_trip(ACTION, clients_moto, REGION, "prod/api")

    assert trip.recorded.status == STATUS_PENDING
    assert trip.recorded.previous_state == {"desired_count": 5}

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"desired_count": 0}

    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"desired_count": 5}


def test_the_applied_state_matches_what_aws_does(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs)
    trip = round_trip(ACTION, clients_moto, REGION, "prod/api")
    assert trip.clamped.applied_state == trip.state_after_clamp


def test_a_dry_run_leaves_the_service_alone(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs, desired=4)
    trip = round_trip(ACTION, clients_moto, REGION, "prod/api", dry_run=True)
    assert trip.state_after_clamp == {"desired_count": 4}


def test_the_clamp_names_both_halves(clients_moto: Any, moto_ecs: Any) -> None:
    make_service(moto_ecs)
    candidate = discover_one(ACTION, clients_moto, REGION, "prod/api")
    assert handler(ACTION).clamp_params(candidate) == {
        "cluster": "prod",
        "service": "api",
        "desiredCount": 0,
    }


# --- release validates the ledger ---------------------------------------


def test_the_release_restores_the_recorded_count() -> None:
    assert handler(ACTION).release_params(entry_with({"desired_count": 7})) == {
        "cluster": "prod",
        "service": "api",
        "desiredCount": 7,
    }


@pytest.mark.parametrize("previous", [{}, {"desired_count": 0}, {"desired_count": "five"}])
def test_a_malformed_recorded_count_is_refused(previous: dict[str, Any]) -> None:
    """Zero included: a release to zero is not a release, and an entry
    recording it is one the clamp should never have produced."""
    with pytest.raises(ValueError):
        handler(ACTION).release_params(entry_with(previous))


def test_live_state_is_none_for_a_service_that_is_gone(clients_moto: Any, moto_ecs: Any) -> None:
    """A deleted service is reported, not treated as released."""
    make_service(moto_ecs)
    moto_ecs.update_service(cluster="prod", service="api", desiredCount=0)
    moto_ecs.delete_service(cluster="prod", service="api")
    state = handler(ACTION).live_state(clients_moto, entry_with({"desired_count": 3}))
    assert state is None


def test_live_state_is_none_for_a_cluster_that_never_existed(clients_moto: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - ECS raises ClusterNotFoundException
        handler(ACTION).live_state(
            clients_moto, entry_with({"desired_count": 1}, target_id="nope/none")
        )
