"""Release: restore exactly what was recorded, and refuse to guess.

Driven against moto with the real actuator and handlers, because the
interesting cases — a conflict, a resource that is gone — are facts about
the live resource and a stub would only assert what the test wrote into it.
"""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import discover_one, handler
from support import REGION
from test_resource_ec2 import launch

from clamper.core.actuator import Actuator, Call
from clamper.core.ledger import (
    STATUS_CLAMPED,
    STATUS_DRY_RUN,
    STATUS_RELEASE_FAILED,
    STATUS_RELEASED,
    Clamp,
    Entry,
    MemoryLedger,
    new_clamp_id,
)
from clamper.core.release import (
    OUTCOME_CONFLICT,
    OUTCOME_GONE,
    OUTCOME_NOT_ACTIVE,
    OUTCOME_RELEASED,
    OUTCOME_UNRESTORABLE,
    OUTCOME_WOULD_RELEASE,
    release,
)

ACTION = "ec2-instance"


def clamp_one(clients: Any, ec2: Any) -> tuple[MemoryLedger, Entry]:
    """Launch, clamp for real, and return the ledger holding the entry."""
    instance_id = launch(ec2)
    candidate = discover_one(ACTION, clients, REGION, instance_id)
    resource = handler(ACTION)
    ledger = MemoryLedger()
    clamp_id = new_clamp_id()
    ledger.open_clamp(
        Clamp(
            clamp_id=clamp_id,
            created_at="2026-11-13T15:30:00+00:00",
            mode="enforce",
            actor="test",
            limit_id="whole-account",
            limit_amount=100.0,
            spend=150.0,
            reason="test",
        )
    )
    entry = ledger.record(
        clamp_id, candidate.target, candidate.state, resource.applied_state(candidate)
    )
    result = Actuator(clients, dry_run=False).invoke(
        Call(
            action_id=ACTION,
            phase="clamp",
            params=resource.clamp_params(candidate),
            target_id=instance_id,
            region=REGION,
        )
    )
    return ledger, ledger.settle(entry, result)


def state_of(ec2: Any, instance_id: str) -> str:
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return str(reservations[0]["Instances"][0]["State"]["Name"])


def test_a_clamped_instance_is_started_again(clients_moto: Any, moto_ec2: Any) -> None:
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    assert state_of(moto_ec2, entry.target.target_id) == "stopped"

    (outcome,) = release(
        [entry], clients=clients_moto, ledger=ledger, actuator=Actuator(clients_moto, dry_run=False)
    )

    assert outcome.outcome == OUTCOME_RELEASED
    assert outcome.forced is False
    assert outcome.entry.status == STATUS_RELEASED
    assert state_of(moto_ec2, entry.target.target_id) == "running"
    assert ledger.active() == ()


def test_a_hand_made_change_is_a_conflict_and_is_left_alone(
    clients_moto: Any, moto_ec2: Any
) -> None:
    """Without applied_state, "it is running" could mean "somebody started
    it" or "the clamp failed". With it, the two are distinguishable."""
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    moto_ec2.start_instances(InstanceIds=[entry.target.target_id])

    (outcome,) = release(
        [entry], clients=clients_moto, ledger=ledger, actuator=Actuator(clients_moto, dry_run=False)
    )

    assert outcome.outcome == OUTCOME_CONFLICT
    assert outcome.needs_attention
    assert "--force" in outcome.detail
    assert "state=running" in outcome.detail
    # Left alone: still clamped in the ledger, nothing called.
    assert [item.status for item in ledger.active()] == [STATUS_CLAMPED]


def test_force_restores_over_a_conflict_and_says_so(clients_moto: Any, moto_ec2: Any) -> None:
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    moto_ec2.start_instances(InstanceIds=[entry.target.target_id])
    moto_ec2.stop_instances(InstanceIds=[entry.target.target_id])
    # Stopped again by hand: the state matches, so no conflict.
    (plain,) = release(
        [entry],
        clients=clients_moto,
        ledger=MemoryLedger(),
        actuator=Actuator(clients_moto, dry_run=True),
    )
    assert plain.forced is False

    moto_ec2.start_instances(InstanceIds=[entry.target.target_id])
    (outcome,) = release(
        [entry],
        clients=clients_moto,
        ledger=ledger,
        actuator=Actuator(clients_moto, dry_run=False),
        force=True,
    )
    assert outcome.outcome == OUTCOME_RELEASED
    assert outcome.forced is True
    assert "overrode a conflict" in outcome.detail


def test_a_resource_that_is_gone_is_never_reported_released(
    clients_moto: Any, moto_ec2: Any
) -> None:
    """A terminated instance is not a restored one. The entry is closed as
    release-failed so it stops appearing as active, with the reason."""
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    moto_ec2.terminate_instances(InstanceIds=[entry.target.target_id])

    (outcome,) = release(
        [entry], clients=clients_moto, ledger=ledger, actuator=Actuator(clients_moto, dry_run=False)
    )
    assert outcome.outcome == OUTCOME_GONE
    assert outcome.needs_attention
    assert outcome.entry.status == STATUS_RELEASE_FAILED
    assert outcome.entry.error_code == "ResourceGone"
    assert ledger.active() == ()


def test_an_unknown_instance_id_is_gone_not_an_error(clients_moto: Any) -> None:
    """EC2 answers an unknown id with InvalidInstanceID.NotFound rather than
    an empty list."""
    from roundtrip import a_target

    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, "i-00000000000000000", REGION),
        status=STATUS_CLAMPED,
        previous_state={"state": "running"},
        applied_state={"state": "stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    assert handler(ACTION).live_state(clients_moto, entry) is None


def test_only_a_clamped_entry_is_released(clients_moto: Any, moto_ec2: Any) -> None:
    """A dry-run or failed entry never stopped anything. Releasing one would
    start a resource Clamper never stopped."""
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    dry = Entry(**{**entry.__dict__, "status": STATUS_DRY_RUN})
    (outcome,) = release(
        [dry], clients=clients_moto, ledger=ledger, actuator=Actuator(clients_moto, dry_run=False)
    )
    assert outcome.outcome == OUTCOME_NOT_ACTIVE
    assert state_of(moto_ec2, entry.target.target_id) == "stopped"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "i-0abc; rm -rf /"),
        ("target_id", "arn:aws:ec2:us-east-1:999999999999:instance/i-0123456789abcdef0"),
        ("region", "us-east-1; curl evil"),
        ("action_id", "ec2-terminate"),
    ],
)
def test_a_tampered_entry_never_reaches_aws(field: str, value: str) -> None:
    """The ledger is untrusted input. A shape the action does not declare is
    reported as unrestorable before any call is built — so no client is
    needed, and passing None proves none was used."""
    target = {
        "action_id": ACTION,
        "target_id": "i-0123456789abcdef0",
        "region": REGION,
        "title": "EC2 instance",
    }
    target[field] = value
    from clamper.core.ledger import Target

    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=Target(**target),
        status=STATUS_CLAMPED,
        previous_state={"state": "running"},
        applied_state={"state": "stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    (outcome,) = release(
        [entry],
        clients=None,
        ledger=MemoryLedger(),
        actuator=_NoCalls(),  # type: ignore[arg-type]
    )
    assert outcome.outcome == OUTCOME_UNRESTORABLE


def test_an_inconsistent_recorded_size_is_unrestorable(
    clients_moto: Any, moto_autoscaling: Any, moto_ec2: Any
) -> None:
    """A recorded DesiredCapacity of 999999 against a MaxSize of 4 fails
    validation; release reports it rather than passing it through."""
    from test_resource_asg import make_group

    make_group(moto_autoscaling, moto_ec2, "web-asg", minimum=1, desired=2, maximum=4)
    from roundtrip import a_target

    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target("autoscaling-group", "web-asg", REGION),
        status=STATUS_CLAMPED,
        previous_state={"min_size": 1, "max_size": 4, "desired_capacity": 999999},
        applied_state={"min_size": 0, "max_size": 4, "desired_capacity": 0},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    moto_autoscaling.update_auto_scaling_group(
        AutoScalingGroupName="web-asg", MinSize=0, DesiredCapacity=0
    )
    (outcome,) = release(
        [entry],
        clients=clients_moto,
        ledger=MemoryLedger(),
        actuator=Actuator(clients_moto, dry_run=False),
    )
    assert outcome.outcome == OUTCOME_UNRESTORABLE
    assert "999999" in outcome.detail


def test_a_dry_run_release_writes_nothing(clients_moto: Any, moto_ec2: Any) -> None:
    ledger, entry = clamp_one(clients_moto, moto_ec2)
    (outcome,) = release(
        [entry], clients=clients_moto, ledger=ledger, actuator=Actuator(clients_moto, dry_run=True)
    )
    assert outcome.outcome == OUTCOME_WOULD_RELEASE
    assert [item.status for item in ledger.active()] == [STATUS_CLAMPED]
    assert state_of(moto_ec2, entry.target.target_id) == "stopped"


def test_one_bad_entry_does_not_stop_the_others(clients_moto: Any, moto_ec2: Any) -> None:
    ledger, good = clamp_one(clients_moto, moto_ec2)
    bad = Entry(**{**good.__dict__, "status": STATUS_DRY_RUN})
    outcomes = release(
        [bad, good],
        clients=clients_moto,
        ledger=ledger,
        actuator=Actuator(clients_moto, dry_run=False),
    )
    assert [item.outcome for item in outcomes] == [OUTCOME_NOT_ACTIVE, OUTCOME_RELEASED]


# --- doubles ------------------------------------------------------------


class _NoCalls:
    """An actuator stand-in that fails the test if anything reaches it."""

    dry_run = False

    def invoke(self, call: Any) -> Any:
        raise AssertionError(f"no call should have been built, got {call}")
