"""A clamp/release round trip, run against moto for every resource type.

Every handler gets the same treatment, because the thing being checked is
the same for all of them and is the tool's central promise:

    discover -> record (write-ahead) -> clamp -> settle
             -> read live state -> release -> mark released
             -> read live state again

and at the end the resource is back the way it started. A handler that
passes this is reversible; one that does not is not, whatever its row in
actions.yml says.

The actuator and the ledger in this harness are the real ones. Only AWS is
replaced, by moto, so that the live state after a clamp is a real state
transition and not a fixture somebody wrote to match.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clamper.core.actions import load_registry
from clamper.core.actuator import Actuator, Call
from clamper.core.ledger import Clamp, Entry, MemoryLedger, Target, new_clamp_id
from clamper.core.resources.base import Candidate, Handler, handler_for
from clamper.core.session import Clients


@dataclass
class RoundTrip:
    """What a round trip saw, so a test can assert on each stage."""

    candidate: Candidate
    recorded: Entry
    clamped: Entry
    state_after_clamp: Mapping[str, Any] | None
    released: Entry
    state_after_release: Mapping[str, Any] | None


def handler(action_id: str) -> Handler:
    return handler_for(load_registry().get(action_id))


def discover_one(action_id: str, clients: Clients, region: str, target_id: str) -> Candidate:
    """The one candidate a test cares about, by id.

    Looking it up rather than taking `[0]`: moto's ordering is not promised,
    and a test that silently asserted against the wrong resource would be
    worse than one that failed.
    """
    found = handler(action_id).discover(clients, region)
    for candidate in found:
        if candidate.target_id == target_id:
            return candidate
    raise AssertionError(
        f"{action_id} did not discover {target_id!r}; found {[item.target_id for item in found]}"
    )


def round_trip(
    action_id: str,
    clients: Clients,
    region: str,
    target_id: str,
    *,
    dry_run: bool = False,
) -> RoundTrip:
    """The whole lifecycle, through the real actuator and a real ledger."""
    resource = handler(action_id)
    ledger = MemoryLedger()
    actuator = Actuator(clients, dry_run=dry_run)

    candidate = discover_one(action_id, clients, region, target_id)
    assert candidate.clampable, f"{target_id} was discovered as not clampable: {candidate.reason}"

    clamp_id = new_clamp_id()
    ledger.open_clamp(
        Clamp(
            clamp_id=clamp_id,
            created_at="2026-11-13T15:30:00+00:00",
            mode="dry-run" if dry_run else "enforce",
            actor="test",
            limit_id="whole-account",
            limit_amount=100.0,
            spend=200.0,
            reason="test",
        )
    )

    # Write-ahead: the previous state is in the ledger before the call.
    recorded = ledger.record(
        clamp_id,
        candidate.target,
        candidate.state,
        resource.applied_state(candidate),
    )

    clamped = ledger.settle(
        recorded,
        actuator.invoke(
            Call(
                action_id=action_id,
                phase="clamp",
                params=resource.clamp_params(candidate),
                target_id=candidate.target_id,
                region=region,
            )
        ),
    )

    state_after_clamp = resource.live_state(clients, clamped)

    released = ledger.mark_released(
        clamped,
        actuator.invoke(
            Call(
                action_id=action_id,
                phase=resource.release_phase(clamped),
                params=resource.release_params(clamped),
                target_id=clamped.target.target_id,
                region=region,
            )
        ),
    )

    return RoundTrip(
        candidate=candidate,
        recorded=recorded,
        clamped=clamped,
        state_after_clamp=state_after_clamp,
        released=released,
        state_after_release=resource.live_state(clients, released),
    )


def a_target(action_id: str, target_id: str, region: str) -> Target:
    return Target(
        action_id=action_id,
        target_id=target_id,
        region=region,
        title=load_registry().get(action_id).title,
    )
