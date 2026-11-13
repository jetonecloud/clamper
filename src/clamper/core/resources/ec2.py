"""EC2 instances: stop, and start the ones Clamper stopped.

The decision worth recording: **an instance that belongs to an Auto Scaling
group is skipped here**, with the group named in the reason, and dealt with
by the `autoscaling-group` action instead.

AWS says so about its own budget actions:

> "If a budget action is used to stop an Amazon EC2 instance in an Auto
> Scaling Group (ASG), Amazon EC2 Auto Scaling restarts the instance, or
> launches new instances to replace the stopped instance."

So stopping one directly would return success, write a `clamped` ledger
entry, send a notification saying the spend had stopped, and then Auto
Scaling would put the instance back within a minute. Every part of that is a
lie except the API response. Scaling the group to zero is the clamp that
actually works.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clamper.core.cost import ec2_price
from clamper.core.ledger import Entry
from clamper.core.resources.base import (
    ASG_TAG,
    Candidate,
    Handler,
    paginate,
    register,
    tag_map,
)
from clamper.core.session import Clients

#: States an instance can be stopped from. `pending` is deliberately absent:
#: AWS refuses to stop an instance that is still starting, so including it
#: would produce a failed clamp instead of a skip with a reason.
STOPPABLE = frozenset({"running"})

#: States that mean somebody or something already stopped it. Not a target,
#: and not a failure either.
ALREADY_STOPPED = frozenset({"stopping", "stopped", "shutting-down", "terminated"})


@register
class Ec2InstanceHandler(Handler):
    action_id = "ec2-instance"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        found: list[Candidate] = []
        # No state filter in the request. An instance that is already stopped
        # has to be *reported* as already stopped rather than being absent,
        # because "we found nothing" and "we found it and it was already off"
        # are different things to tell somebody looking at a bill.
        for page in paginate(clients, "ec2", "describe_instances", region):
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    candidate = self._candidate(instance, region)
                    if candidate is not None:
                        found.append(candidate)
        return found

    def _candidate(self, instance: Mapping[str, Any], region: str) -> Candidate | None:
        instance_id = str(instance.get("InstanceId") or "")
        if not instance_id:
            return None

        tags = tag_map(instance.get("Tags"))
        state = str(instance.get("State", {}).get("Name") or "unknown")
        instance_type = str(instance.get("InstanceType") or "unknown")
        price = ec2_price(instance_type)
        description = f"{instance_type}, {state}"

        if group := tags.get(ASG_TAG):
            return self.unclampable(
                target_id=instance_id,
                region=region,
                description=description,
                tags=tags,
                reason=(
                    f"belongs to the Auto Scaling group {group}, which would relaunch "
                    f"it. Clamp the group instead — tag it clamper:managed and the "
                    f"autoscaling-group action takes it to zero."
                ),
                price=price,
            )

        if state in ALREADY_STOPPED:
            return self.unclampable(
                target_id=instance_id,
                region=region,
                description=description,
                tags=tags,
                reason=f"already {state}",
                price=price,
            )

        if state not in STOPPABLE:
            return self.unclampable(
                target_id=instance_id,
                region=region,
                description=description,
                tags=tags,
                reason=f"state is {state}; AWS only accepts a stop from running",
                price=price,
            )

        if instance.get("SpotInstanceRequestId"):
            # A stopped Spot instance only restarts if there is capacity at
            # the time, so the release is not reliable. Reported rather than
            # attempted: an action Clamper cannot undo on demand does not
            # belong in v1.
            return self.unclampable(
                target_id=instance_id,
                region=region,
                description=f"{description}, Spot",
                tags=tags,
                reason=(
                    "a Spot instance only restarts when capacity is available, so "
                    "the release is not guaranteed. v1 clamps only what it can put "
                    "back on demand."
                ),
                price=price,
            )

        return self.candidate(
            target_id=instance_id,
            region=region,
            description=description,
            tags=tags,
            state={"state": state},
            price=price,
        )

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("ec2", region=entry.target.region)
        response = client.describe_instances(InstanceIds=[entry.target.target_id])
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                return {"state": str(instance.get("State", {}).get("Name") or "unknown")}
        return None

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {"InstanceIds": [candidate.target_id]}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        # `stopped`, not `stopping`. A release reads the live state and
        # compares it with this, and by the time anybody releases, a
        # successfully stopped instance is `stopped`. Recording the
        # transitional state would make every release look like a conflict.
        return {"state": "stopped"}

    def release_params(self, entry: Entry) -> dict[str, Any]:
        return {"InstanceIds": [entry.target.target_id]}
