"""Auto Scaling groups: scale to zero, restore all three sizes."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import REGION

from clamper.core.ledger import STATUS_CLAMPED, STATUS_PENDING, STATUS_RELEASED, Entry

ACTION = "autoscaling-group"


def make_group(
    autoscaling: Any,
    ec2: Any,
    name: str = "web",
    *,
    minimum: int = 2,
    maximum: int = 10,
    desired: int = 4,
    tags: dict[str, str] | None = None,
) -> str:
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    autoscaling.create_launch_configuration(
        LaunchConfigurationName=f"{name}-lc",
        ImageId=image,
        InstanceType="m5.large",
    )
    autoscaling.create_auto_scaling_group(
        AutoScalingGroupName=name,
        LaunchConfigurationName=f"{name}-lc",
        MinSize=minimum,
        MaxSize=maximum,
        DesiredCapacity=desired,
        AvailabilityZones=[f"{REGION}a"],
        Tags=[
            {"Key": key, "Value": value, "PropagateAtLaunch": True}
            for key, value in (tags or {}).items()
        ],
    )
    return name


def entry_with(previous: dict[str, Any], *, name: str = "web") -> Entry:
    return Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, name, REGION),
        status=STATUS_CLAMPED,
        previous_state=previous,
        applied_state={"min_size": 0, "max_size": 10, "desired_capacity": 0},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )


# --- discovery ----------------------------------------------------------


def test_a_group_with_capacity_is_a_candidate(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2, tags={"clamper:managed": "true"})
    candidate = discover_one(ACTION, clients_moto, REGION, "web")

    assert candidate.clampable is True
    assert candidate.state == {"min_size": 2, "max_size": 10, "desired_capacity": 4}
    assert candidate.description == "min 2 / desired 4 / max 10"
    assert candidate.tags["clamper:managed"] == "true"


def test_a_group_already_at_zero_is_reported_not_clamped(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2, minimum=0, desired=0)
    candidate = discover_one(ACTION, clients_moto, REGION, "web")
    assert candidate.clampable is False
    assert candidate.reason == "already at zero"


def test_the_group_price_sums_its_instances(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """So the expensive group goes to the top of the plan."""
    make_group(moto_autoscaling, moto_ec2, desired=3, minimum=1)
    candidate = discover_one(ACTION, clients_moto, REGION, "web")
    # 3 x m5.large at 0.096.
    assert candidate.hourly_usd == pytest.approx(0.288)
    assert "3x m5.large" in (candidate.price.basis if candidate.price else "")


def test_an_unknown_instance_type_means_no_group_price(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """A partial sum would understate the saving, and the figure's whole job
    is to put the expensive group first — an understated one does the
    opposite."""
    image = moto_ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    moto_autoscaling.create_launch_configuration(
        LaunchConfigurationName="odd-lc", ImageId=image, InstanceType="m7i.48xlarge"
    )
    moto_autoscaling.create_auto_scaling_group(
        AutoScalingGroupName="odd",
        LaunchConfigurationName="odd-lc",
        MinSize=1,
        MaxSize=2,
        DesiredCapacity=1,
        AvailabilityZones=[f"{REGION}a"],
    )
    candidate = discover_one(ACTION, clients_moto, REGION, "odd")
    assert candidate.price is None


def test_a_suspended_process_is_mentioned_in_the_description(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """A suspended `Launch` means the release will not bring instances back
    until somebody resumes it — a release reported as done that does not
    restore service."""
    make_group(moto_autoscaling, moto_ec2)
    moto_autoscaling.suspend_processes(AutoScalingGroupName="web", ScalingProcesses=["Launch"])
    candidate = discover_one(ACTION, clients_moto, REGION, "web")
    assert "suspended" in candidate.description
    assert candidate.clampable is True


# --- the clamp ----------------------------------------------------------


def test_the_clamp_sets_min_size_as_well_as_desired(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """Desired alone leaves the group under its minimum and Auto Scaling
    launches instances to get back to it, which presents as the clamp
    failing for no reason because the API call succeeded."""
    make_group(moto_autoscaling, moto_ec2)
    candidate = discover_one(ACTION, clients_moto, REGION, "web")
    params = handler(ACTION).clamp_params(candidate)
    assert params == {"AutoScalingGroupName": "web", "MinSize": 0, "DesiredCapacity": 0}


def test_the_applied_state_keeps_the_max_size(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """The clamp does not touch MaxSize, so the state after it still has the
    group's own maximum. Recording zero would make every release a conflict."""
    make_group(moto_autoscaling, moto_ec2)
    candidate = discover_one(ACTION, clients_moto, REGION, "web")
    assert handler(ACTION).applied_state(candidate) == {
        "min_size": 0,
        "max_size": 10,
        "desired_capacity": 0,
    }


# --- the round trip -----------------------------------------------------


def test_clamp_and_release_round_trip(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2, minimum=2, maximum=10, desired=4)
    trip = round_trip(ACTION, clients_moto, REGION, "web")

    assert trip.recorded.status == STATUS_PENDING
    assert trip.recorded.previous_state == {
        "min_size": 2,
        "max_size": 10,
        "desired_capacity": 4,
    }

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"min_size": 0, "max_size": 10, "desired_capacity": 0}

    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {
        "min_size": 2,
        "max_size": 10,
        "desired_capacity": 4,
    }


def test_the_applied_state_matches_what_aws_does(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2)
    trip = round_trip(ACTION, clients_moto, REGION, "web")
    assert trip.clamped.applied_state == trip.state_after_clamp


def test_a_dry_run_leaves_the_group_alone(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2)
    trip = round_trip(ACTION, clients_moto, REGION, "web", dry_run=True)
    assert trip.state_after_clamp == {"min_size": 2, "max_size": 10, "desired_capacity": 4}


# --- release validates the ledger ---------------------------------------


def test_the_release_restores_all_four_parameters(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    make_group(moto_autoscaling, moto_ec2)
    params = handler(ACTION).release_params(
        entry_with({"min_size": 2, "max_size": 10, "desired_capacity": 4})
    )
    assert params == {
        "AutoScalingGroupName": "web",
        "MinSize": 2,
        "MaxSize": 10,
        "DesiredCapacity": 4,
    }


@pytest.mark.parametrize(
    "previous",
    [
        {"min_size": 2, "max_size": 10},
        {"min_size": "two", "max_size": 10, "desired_capacity": 4},
        {"min_size": -1, "max_size": 10, "desired_capacity": 4},
        {"min_size": 2, "max_size": 10, "desired_capacity": None},
    ],
)
def test_a_malformed_ledger_entry_is_refused(previous: dict[str, Any]) -> None:
    """A ledger entry is untrusted input — the threat model covers somebody
    with table write access putting a nonsense size in one. A release builds
    a real AWS call from these values, so they are checked."""
    with pytest.raises(ValueError):
        handler(ACTION).release_params(entry_with(previous))


def test_inconsistent_recorded_sizes_are_refused_with_a_useful_message() -> None:
    """AWS would refuse it too, but refusing here means the report says "the
    recorded sizes are inconsistent" rather than quoting a ValidationError."""
    with pytest.raises(ValueError) as excinfo:
        handler(ACTION).release_params(
            entry_with({"min_size": 2, "max_size": 3, "desired_capacity": 9})
        )
    assert "unrestorable" in str(excinfo.value)


def test_live_state_is_none_for_a_group_that_is_gone(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """A deleted group is reported, not treated as released."""
    state = handler(ACTION).live_state(clients_moto, entry_with({}, name="never-existed"))
    assert state is None
