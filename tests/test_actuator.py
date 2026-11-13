"""The one write path, exercised against a validated fake.

`Stubber` validates every request against the real service model, so a
wrong parameter name fails here rather than in somebody's account. Behaviour
tests only; the structural guarantees are in `test_safety.py`.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from clamper.core.actions import PHASE_CLAMP, PHASE_RELEASE, UnknownAction, load_registry
from clamper.core.actuator import Actuator, Call, ForbiddenCall, clamp_call
from clamper.core.session import Clients

REGION = "us-east-1"


@pytest.fixture()
def ec2() -> Any:
    return boto3.client("ec2", region_name=REGION, aws_access_key_id="a", aws_secret_access_key="b")


def actuator_for(service: str, client: Any, *, dry_run: bool = False) -> Actuator:
    """An actuator whose client cache is pre-seeded with a stubbed client.

    Deliberately the real `Clients` and the real `Actuator` — the thing under
    test is the dispatch, so replacing either would test nothing.
    """
    session = boto3.Session(region_name=REGION)
    clients = Clients(session, default_region=REGION)
    clients.stub(service, client, region=REGION)
    return Actuator(clients, dry_run=dry_run)


# --- the happy path -----------------------------------------------------


def test_a_clamp_reaches_aws_with_the_declared_parameters(ec2: Any) -> None:
    stub = Stubber(ec2)
    stub.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": "i-1", "CurrentState": {"Name": "stopping"}}]},
        {"InstanceIds": ["i-1"]},
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        result = actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION)
        )
    assert result.ok
    assert result.performed
    assert not result.dry_run
    assert result.summary == "done"
    assert actuator.performed == (result,)


def test_a_release_uses_the_inverse_method(ec2: Any) -> None:
    stub = Stubber(ec2)
    stub.add_response(
        "start_instances",
        {"StartingInstances": [{"InstanceId": "i-1", "CurrentState": {"Name": "pending"}}]},
        {"InstanceIds": ["i-1"]},
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        result = actuator.invoke(
            Call(
                action_id="ec2-instance",
                phase=PHASE_RELEASE,
                params={"InstanceIds": ["i-1"]},
                target_id="i-1",
                region=REGION,
            )
        )
    assert result.performed


def test_response_metadata_is_dropped_from_the_result(ec2: Any) -> None:
    """A ledger entry should be a few hundred bytes, not a request trace."""
    stub = Stubber(ec2)
    stub.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": "i-1", "CurrentState": {"Name": "stopping"}}]},
        {"InstanceIds": ["i-1"]},
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        result = actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION)
        )
    assert "ResponseMetadata" not in result.response
    assert "StoppingInstances" in result.response


# --- dry run ------------------------------------------------------------


def test_dry_run_makes_no_call_at_all(ec2: Any) -> None:
    """The most important test in this file.

    An empty `Stubber` raises on any request, so the stub itself is the
    assertion: if dry-run reached boto3, this fails.
    """
    stub = Stubber(ec2)
    actuator = actuator_for("ec2", ec2, dry_run=True)
    with stub:
        result = actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION)
        )
    stub.assert_no_pending_responses()
    assert result.dry_run
    assert result.ok
    assert result.performed is False
    assert result.summary == "would clamp (dry-run)"


def test_dry_run_records_nothing_as_performed(ec2: Any) -> None:
    """`performed` is what every counter uses, and a dry run must not add to
    it. "Would have worked" and "worked" are different claims."""
    actuator = actuator_for("ec2", ec2, dry_run=True)
    for index in range(5):
        actuator.invoke(
            clamp_call("ec2-instance", f"i-{index}", {"InstanceIds": [f"i-{index}"]}, REGION)
        )
    assert actuator.performed == ()


def test_dry_run_still_validates_parameters(ec2: Any) -> None:
    """So `clamper simulate` and a dry-run enforcer catch a malformed call
    rather than silently approving it. A dry run that validates nothing is a
    dry run that proves nothing."""
    actuator = actuator_for("ec2", ec2, dry_run=True)
    with pytest.raises(ForbiddenCall):
        actuator.invoke(clamp_call("ec2-instance", "i-1", {"Wrong": ["i-1"]}, REGION))


def test_dry_run_cannot_be_turned_off_after_construction(ec2: Any) -> None:
    """A mode that can change mid-run is a mode a bug can change."""
    actuator = actuator_for("ec2", ec2, dry_run=True)
    with pytest.raises(AttributeError):
        actuator.dry_run = False  # type: ignore[misc]


# --- refusals -----------------------------------------------------------


def test_an_unknown_action_id_is_refused(ec2: Any) -> None:
    """How a ledger entry naming a retired type surfaces, rather than as a
    silent no-op."""
    actuator = actuator_for("ec2", ec2)
    with pytest.raises(UnknownAction):
        actuator.invoke(clamp_call("ec2-obliterate", "i-1", {"InstanceIds": ["i-1"]}, REGION))


def test_an_unknown_phase_is_refused(ec2: Any) -> None:
    actuator = actuator_for("ec2", ec2)
    with pytest.raises(UnknownAction):
        actuator.invoke(
            Call(
                action_id="ec2-instance",
                phase="terminate",
                params={"InstanceIds": ["i-1"]},
                target_id="i-1",
                region=REGION,
            )
        )


def test_release_absent_on_an_action_without_one_is_refused(ec2: Any) -> None:
    actuator = actuator_for("ec2", ec2)
    with pytest.raises(UnknownAction):
        actuator.invoke(
            Call(
                action_id="ec2-instance",
                phase="release_absent",
                params={"InstanceIds": ["i-1"]},
                target_id="i-1",
                region=REGION,
            )
        )


def test_an_extra_parameter_is_refused(ec2: Any) -> None:
    """`Force=True` arriving as an unchecked passthrough is the shape of this
    whole class of bug."""
    actuator = actuator_for("ec2", ec2)
    with pytest.raises(ForbiddenCall) as excinfo:
        actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"], "Force": True}, REGION)
        )
    assert "Force" in str(excinfo.value)
    assert "nobody reviewed" in str(excinfo.value)


def test_a_missing_parameter_is_refused(ec2: Any) -> None:
    """The Auto Scaling case is the reason: a release that omits MinSize
    restores the desired capacity under a minimum of zero."""
    actuator = actuator_for("autoscaling", ec2)
    with pytest.raises(ForbiddenCall) as excinfo:
        actuator.invoke(
            Call(
                action_id="autoscaling-group",
                phase=PHASE_RELEASE,
                params={"AutoScalingGroupName": "web", "DesiredCapacity": 3},
                target_id="web",
                region=REGION,
            )
        )
    message = str(excinfo.value)
    assert "MinSize" in message
    assert "MaxSize" in message


def test_a_refusal_happens_before_any_request(ec2: Any) -> None:
    """An empty Stubber is the assertion again."""
    stub = Stubber(ec2)
    actuator = actuator_for("ec2", ec2)
    with stub, pytest.raises(ForbiddenCall):
        actuator.invoke(clamp_call("ec2-instance", "i-1", {"Nope": 1}, REGION))
    stub.assert_no_pending_responses()


def test_the_allowlist_is_the_registrys(ec2: Any) -> None:
    assert actuator_for("ec2", ec2).allowed_methods == load_registry().methods


# --- failures are results, not exceptions -------------------------------


def test_an_aws_refusal_comes_back_as_a_failed_result(ec2: Any) -> None:
    """Never raised. One target Clamper cannot stop must not stop it trying
    the rest — and must never be counted as done."""
    stub = Stubber(ec2)
    stub.add_client_error(
        "stop_instances",
        service_error_code="UnauthorizedOperation",
        service_message="You are not authorized to perform this operation.",
        http_status_code=403,
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        result = actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION)
        )
    assert result.ok is False
    assert result.performed is False
    assert result.error_code == "UnauthorizedOperation"
    assert "not authorized" in (result.error_message or "")
    assert "failed" in result.summary


def test_a_failed_call_is_still_recorded_as_attempted(ec2: Any) -> None:
    """So a run that crashes later can still report what it tried."""
    stub = Stubber(ec2)
    stub.add_client_error("stop_instances", service_error_code="IncorrectInstanceState")
    actuator = actuator_for("ec2", ec2)
    with stub:
        actuator.invoke(clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION))
    assert len(actuator.performed) == 1
    assert actuator.performed[0].ok is False


def test_a_long_error_message_is_trimmed(ec2: Any) -> None:
    """A ledger entry and a notification are both places a 4 KB AWS error
    quoting a request would be unhelpful."""
    stub = Stubber(ec2)
    stub.add_client_error(
        "stop_instances", service_error_code="ValidationError", service_message="x" * 4000
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        result = actuator.invoke(
            clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION)
        )
    assert result.error_message is not None
    assert len(result.error_message) == 500


def test_one_failure_does_not_prevent_the_next_call(ec2: Any) -> None:
    stub = Stubber(ec2)
    stub.add_client_error("stop_instances", service_error_code="IncorrectInstanceState")
    stub.add_response(
        "stop_instances",
        {"StoppingInstances": [{"InstanceId": "i-2", "CurrentState": {"Name": "stopping"}}]},
        {"InstanceIds": ["i-2"]},
    )
    actuator = actuator_for("ec2", ec2)
    with stub:
        first = actuator.invoke(clamp_call("ec2-instance", "i-1", {"InstanceIds": ["i-1"]}, REGION))
        second = actuator.invoke(
            clamp_call("ec2-instance", "i-2", {"InstanceIds": ["i-2"]}, REGION)
        )
    assert first.ok is False
    assert second.performed is True


# --- the call object ----------------------------------------------------


def test_a_call_labels_itself_readably() -> None:
    call = clamp_call("rds-instance", "prod-db", {"DBInstanceIdentifier": "prod-db"}, REGION)
    assert call.label == "rds-instance.clamp on prod-db"
    assert call.phase == PHASE_CLAMP


def test_a_call_cannot_carry_a_method_name() -> None:
    """The structural reason the actuator is safe: there is no field for one.

    If this assertion ever fails because somebody added a `method` field,
    the single write path is gone and the allowlist is decoration.
    """
    assert not hasattr(clamp_call("ec2-instance", "i-1", {}, REGION), "method")
    assert set(Call.__dataclass_fields__) == {
        "action_id",
        "phase",
        "params",
        "target_id",
        "region",
    }
