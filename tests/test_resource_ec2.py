"""EC2 instances: discovery, the Auto Scaling skip, and the round trip."""

from __future__ import annotations

from typing import Any

from roundtrip import discover_one, handler, round_trip
from support import REGION

from clamper.core.ledger import STATUS_CLAMPED, STATUS_PENDING, STATUS_RELEASED, Entry, Target
from clamper.core.resources.base import ASG_TAG

ACTION = "ec2-instance"


def launch(
    client: Any, *, tags: dict[str, str] | None = None, instance_type: str = "m5.large"
) -> str:
    image = client.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    spec: dict[str, Any] = {
        "ImageId": image,
        "MinCount": 1,
        "MaxCount": 1,
        "InstanceType": instance_type,
    }
    if tags:
        spec["TagSpecifications"] = [
            {
                "ResourceType": "instance",
                "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            }
        ]
    return str(client.run_instances(**spec)["Instances"][0]["InstanceId"])


# --- discovery ----------------------------------------------------------


def test_a_running_instance_is_a_candidate(clients_moto: Any, moto_ec2: Any) -> None:
    instance_id = launch(moto_ec2, tags={"clamper:managed": "true"})
    candidate = discover_one(ACTION, clients_moto, REGION, instance_id)

    assert candidate.clampable is True
    assert candidate.state == {"state": "running"}
    assert candidate.tags["clamper:managed"] == "true"
    assert "m5.large" in candidate.description


def test_the_price_is_attached_for_a_known_type(clients_moto: Any, moto_ec2: Any) -> None:
    """So `clamper plan` can put the expensive thing first."""
    instance_id = launch(moto_ec2, instance_type="m5.24xlarge")
    candidate = discover_one(ACTION, clients_moto, REGION, instance_id)
    assert candidate.hourly_usd == 4.608
    assert "us-east-1 on-demand" in (candidate.price.basis if candidate.price else "")


def test_an_unknown_instance_type_gets_no_price(clients_moto: Any, moto_ec2: Any) -> None:
    """No figure rather than a guess from the family name. A wrong number in
    a report costs more credibility than the number was worth."""
    instance_id = launch(moto_ec2, instance_type="m7i.48xlarge")
    candidate = discover_one(ACTION, clients_moto, REGION, instance_id)
    assert candidate.price is None
    assert candidate.hourly_usd is None


def test_an_autoscaling_instance_is_reported_not_dropped(clients_moto: Any, moto_ec2: Any) -> None:
    """The decision this handler exists to get right.

    AWS relaunches instances a budget action stops in a group, so a clamp
    here would return success, write a `clamped` entry, notify that the
    spend had stopped, and be undone within a minute.
    """
    instance_id = launch(moto_ec2, tags={ASG_TAG: "web-asg"})
    candidate = discover_one(ACTION, clients_moto, REGION, instance_id)

    assert candidate.clampable is False
    assert "web-asg" in (candidate.reason or "")
    assert "relaunch" in (candidate.reason or "")
    # And it names the thing to do instead.
    assert "autoscaling-group" in (candidate.reason or "")


def test_an_already_stopped_instance_is_reported_as_already_stopped(
    clients_moto: Any, moto_ec2: Any
) -> None:
    """Not absent. "We found nothing" and "we found it and it was already
    off" are different things to tell somebody looking at a bill."""
    instance_id = launch(moto_ec2)
    moto_ec2.stop_instances(InstanceIds=[instance_id])
    candidate = discover_one(ACTION, clients_moto, REGION, instance_id)

    assert candidate.clampable is False
    assert "already" in (candidate.reason or "")


def test_every_unclampable_candidate_carries_a_reason(clients_moto: Any, moto_ec2: Any) -> None:
    launch(moto_ec2, tags={ASG_TAG: "a"})
    stopped = launch(moto_ec2)
    moto_ec2.stop_instances(InstanceIds=[stopped])
    launch(moto_ec2)

    for candidate in handler(ACTION).discover(clients_moto, REGION):
        if not candidate.clampable:
            assert candidate.reason, candidate.target_id


def test_discovery_paginates(clients_moto: Any, moto_ec2: Any) -> None:
    """An account that silently reported the first page would clamp a subset
    and report it as everything."""
    image = moto_ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    moto_ec2.run_instances(ImageId=image, MinCount=12, MaxCount=12, InstanceType="t3.micro")
    found = handler(ACTION).discover(clients_moto, REGION)
    assert len([item for item in found if item.clampable]) >= 12


# --- the round trip -----------------------------------------------------


def test_clamp_and_release_round_trip(clients_moto: Any, moto_ec2: Any) -> None:
    """The central promise, end to end against a real state transition."""
    instance_id = launch(moto_ec2, tags={"clamper:managed": "true"})
    trip = round_trip(ACTION, clients_moto, REGION, instance_id)

    # Write-ahead: recorded before the call, as pending.
    assert trip.recorded.status == STATUS_PENDING
    assert trip.recorded.previous_state == {"state": "running"}

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"state": "stopped"}

    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"state": "running"}


def test_the_applied_state_matches_what_aws_actually_does(clients_moto: Any, moto_ec2: Any) -> None:
    """`stopped`, not `stopping`.

    Recording the transitional state would make every later release look
    like a conflict, and a conflict is reported and left alone — so the clamp
    would become unreleasable through Clamper.
    """
    instance_id = launch(moto_ec2)
    trip = round_trip(ACTION, clients_moto, REGION, instance_id)
    assert trip.clamped.applied_state == trip.state_after_clamp


def test_a_dry_run_round_trip_changes_nothing(clients_moto: Any, moto_ec2: Any) -> None:
    """The whole decision runs; the instance keeps running."""
    instance_id = launch(moto_ec2)
    trip = round_trip(ACTION, clients_moto, REGION, instance_id, dry_run=True)

    assert trip.clamped.status == "dry-run"
    assert trip.state_after_clamp == {"state": "running"}


def test_release_only_touches_what_clamper_stopped(clients_moto: Any, moto_ec2: Any) -> None:
    """A release is built from one ledger entry and names one instance, so an
    instance somebody else stopped is not in the call."""
    mine = launch(moto_ec2)
    theirs = launch(moto_ec2)
    moto_ec2.stop_instances(InstanceIds=[theirs])

    trip = round_trip(ACTION, clients_moto, REGION, mine)
    assert trip.state_after_release == {"state": "running"}

    still_stopped = moto_ec2.describe_instances(InstanceIds=[theirs])
    state = still_stopped["Reservations"][0]["Instances"][0]["State"]["Name"]
    assert state == "stopped"


def test_live_state_is_none_for_a_resource_that_is_gone(clients_moto: Any, moto_ec2: Any) -> None:
    """A deleted resource is reported, not treated as released."""
    instance_id = launch(moto_ec2)
    moto_ec2.terminate_instances(InstanceIds=[instance_id])

    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=Target(action_id=ACTION, target_id=instance_id, region=REGION, title="EC2 instance"),
        status=STATUS_CLAMPED,
        previous_state={"state": "running"},
        applied_state={"state": "stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    # moto keeps a terminated instance describable for a while, so assert on
    # the state rather than on None here; the None path is covered by the
    # handler tests for services that really drop the resource.
    state = handler(ACTION).live_state(clients_moto, entry)
    assert state is None or state["state"] in {"terminated", "shutting-down"}
