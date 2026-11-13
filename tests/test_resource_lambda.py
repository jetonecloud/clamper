"""Lambda functions: the two-shaped release, and the round trip for both."""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import ACCOUNT, REGION

from clamper.core.actions import PHASE_RELEASE, PHASE_RELEASE_ABSENT
from clamper.core.ledger import STATUS_CLAMPED, STATUS_RELEASED, Entry
from clamper.core.resources.lambda_ import FIELD

ACTION = "lambda-function"


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.py", "def handler(event, context):\n    return 1\n")
    return buffer.getvalue()


def make_function(
    aws_client: Callable[[str], Any],
    name: str = "retry-loop",
    *,
    reserved: int | None = None,
    tags: dict[str, str] | None = None,
) -> str:
    iam = aws_client("iam")
    with contextlib.suppress(iam.exceptions.EntityAlreadyExistsException):
        iam.create_role(
            RoleName="fn-role",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )
    client = aws_client("lambda")
    client.create_function(
        FunctionName=name,
        Runtime="python3.12",
        Role=f"arn:aws:iam::{ACCOUNT}:role/fn-role",
        Handler="index.handler",
        Code={"ZipFile": _zip()},
        MemorySize=256,
        Tags=tags or {},
    )
    if reserved is not None:
        client.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=reserved)
    return name


def entry_with(previous: dict[str, Any]) -> Entry:
    return Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, "retry-loop", REGION),
        status=STATUS_CLAMPED,
        previous_state=previous,
        applied_state={FIELD: 0},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )


# --- discovery ----------------------------------------------------------


def test_a_function_without_a_reservation_records_none(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    """None, not zero and not a missing key. Zero is a real reservation and
    a missing key is a malformed entry."""
    make_function(aws_client, tags={"clamper:managed": "true"})
    candidate = discover_one(ACTION, clients_moto, REGION, "retry-loop")

    assert candidate.clampable is True
    assert candidate.state == {FIELD: None}
    assert candidate.tags["clamper:managed"] == "true"
    assert "no reserved concurrency" in candidate.description


def test_a_function_with_a_reservation_records_it(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    make_function(aws_client, reserved=20)
    candidate = discover_one(ACTION, clients_moto, REGION, "retry-loop")
    assert candidate.state == {FIELD: 20}


def test_a_function_already_at_zero_is_reported(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    make_function(aws_client, reserved=0)
    candidate = discover_one(ACTION, clients_moto, REGION, "retry-loop")
    assert candidate.clampable is False
    assert "already zero" in (candidate.reason or "")


def test_no_price_is_claimed(clients_moto: Any, aws_client: Callable[[str], Any]) -> None:
    make_function(aws_client)
    assert discover_one(ACTION, clients_moto, REGION, "retry-loop").price is None


# --- the round trips ----------------------------------------------------


def test_round_trip_for_a_function_with_no_reservation(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    """The release removes the setting rather than inventing a number."""
    make_function(aws_client)
    trip = round_trip(ACTION, clients_moto, REGION, "retry-loop")

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {FIELD: 0}
    assert trip.clamped.applied_state == trip.state_after_clamp
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {FIELD: None}


def test_round_trip_for_a_function_with_a_reservation(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    make_function(aws_client, reserved=25)
    trip = round_trip(ACTION, clients_moto, REGION, "retry-loop")

    assert trip.state_after_clamp == {FIELD: 0}
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {FIELD: 25}


def test_a_dry_run_leaves_the_function_alone(
    clients_moto: Any, aws_client: Callable[[str], Any]
) -> None:
    make_function(aws_client, reserved=5)
    trip = round_trip(ACTION, clients_moto, REGION, "retry-loop", dry_run=True)
    assert trip.state_after_clamp == {FIELD: 5}


# --- the release, from the ledger ---------------------------------------


def test_an_absent_previous_value_uses_release_absent() -> None:
    entry = entry_with({FIELD: None})
    assert handler(ACTION).release_phase(entry) == PHASE_RELEASE_ABSENT
    assert handler(ACTION).release_params(entry) == {"FunctionName": "retry-loop"}


def test_a_recorded_value_uses_the_ordinary_release() -> None:
    entry = entry_with({FIELD: 12})
    assert handler(ACTION).release_phase(entry) == PHASE_RELEASE
    assert handler(ACTION).release_params(entry) == {
        "FunctionName": "retry-loop",
        "ReservedConcurrentExecutions": 12,
    }


def test_a_missing_field_is_not_guessed_to_be_absent() -> None:
    """Guessing "absent" would delete a reservation somebody may have set on
    purpose. A malformed entry is reported as unrestorable instead."""
    with pytest.raises(ValueError, match="will not guess"):
        handler(ACTION).release_phase(entry_with({}))


@pytest.mark.parametrize("value", [0, -3, "lots", True])
def test_a_bad_recorded_value_is_refused(value: Any) -> None:
    """A ledger entry is untrusted input and this number becomes a real
    reservation. Zero included: a recorded reservation of zero means the
    function was already throttled, and such a candidate is never clamped."""
    with pytest.raises(ValueError):
        handler(ACTION).release_params(entry_with({FIELD: value}))


def test_live_state_is_none_for_a_function_that_is_gone(clients_moto: Any) -> None:
    assert handler(ACTION).live_state(clients_moto, entry_with({FIELD: None})) is None
