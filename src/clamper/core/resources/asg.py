"""Auto Scaling groups: scale to zero, and restore the recorded sizes.

Why the group and not its instances: AWS documents that Auto Scaling
relaunches an instance a budget action stops, or launches a replacement. So
the only clamp that holds is one that changes what the group is *trying* to
run.

**`MinSize` goes to zero as well as `DesiredCapacity`.** Setting desired
alone leaves the group below its minimum, and Auto Scaling launches
instances to get back to it — which presents as the clamp failing for no
visible reason, because the API call succeeded. `actions.yml` declares both
parameters for that reason and the actuator rejects a call that omits
either.

`MaxSize` is not changed by the clamp but *is* restored by the release. The
release declares all four parameters so that a partial update cannot leave
the group with whatever the clamp's partial update left behind.

The honest cost of this action, in `actions.yml` notes and in every plan:
scaling to zero **terminates** the group's instances. That is Auto Scaling's
behaviour, not Clamper's, and it is why only the group's sizes are
restorable. Anything holding state on those instances loses it. A group
whose instances are not disposable should carry `clamper:protect`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from clamper.core.cost import Price, ec2_price
from clamper.core.ledger import Entry
from clamper.core.resources.base import (
    Candidate,
    Handler,
    int_or_none,
    paginate,
    register,
    require_int,
    tag_map,
)
from clamper.core.session import Clients


@register
class AutoScalingGroupHandler(Handler):
    action_id = "autoscaling-group"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        found: list[Candidate] = []
        for page in paginate(clients, "autoscaling", "describe_auto_scaling_groups", region):
            for group in page.get("AutoScalingGroups", []):
                candidate = self._candidate(group, region)
                if candidate is not None:
                    found.append(candidate)
        return found

    def _candidate(self, group: Mapping[str, Any], region: str) -> Candidate | None:
        name = str(group.get("AutoScalingGroupName") or "")
        if not name:
            return None

        tags = tag_map(group.get("Tags"))
        minimum = int_or_none(group.get("MinSize")) or 0
        maximum = int_or_none(group.get("MaxSize")) or 0
        desired = int_or_none(group.get("DesiredCapacity")) or 0
        instances = group.get("Instances") or []
        price = self._price(instances)
        description = f"min {minimum} / desired {desired} / max {maximum}"

        if desired == 0 and minimum == 0:
            return self.unclampable(
                target_id=name,
                region=region,
                description=description,
                tags=tags,
                reason="already at zero",
                price=price,
            )

        if group.get("SuspendedProcesses"):
            suspended = ", ".join(
                str(item.get("ProcessName") or "") for item in group["SuspendedProcesses"]
            )
            # Not a refusal: the clamp still works, because changing the
            # group's sizes is a configuration change rather than a scaling
            # activity. It is worth saying, though, because a suspended
            # `Launch` means the release will not bring instances back until
            # somebody resumes it — and that is a release reported as done
            # that does not restore service.
            description = f"{description} (suspended: {suspended})"

        return self.candidate(
            target_id=name,
            region=region,
            description=description,
            tags=tags,
            state={
                "min_size": minimum,
                "max_size": maximum,
                "desired_capacity": desired,
            },
            price=price,
        )

    def _price(self, instances: Sequence[Mapping[str, Any]]) -> Price | None:
        """The group's current instance-hours, summed.

        `None` unless every running instance's type is in the price table. A
        partial sum would understate the saving, and the whole point of the
        figure is to put the expensive group at the top of the plan — an
        understated one does the opposite of its job.
        """
        if not instances:
            return None
        total = 0.0
        types: dict[str, int] = {}
        for instance in instances:
            instance_type = str(instance.get("InstanceType") or "")
            price = ec2_price(instance_type) if instance_type else None
            if price is None:
                return None
            total += price.hourly_usd
            types[instance_type] = types.get(instance_type, 0) + 1
        shape = ", ".join(f"{count}x {name}" for name, count in sorted(types.items()))
        return Price(
            hourly_usd=round(total, 6),
            basis=f"{shape}, us-east-1 on-demand list price, excluding storage",
        )

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("autoscaling", region=entry.target.region)
        response = client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[entry.target.target_id]
        )
        for group in response.get("AutoScalingGroups", []):
            return {
                "min_size": int_or_none(group.get("MinSize")) or 0,
                "max_size": int_or_none(group.get("MaxSize")) or 0,
                "desired_capacity": int_or_none(group.get("DesiredCapacity")) or 0,
            }
        return None

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {
            "AutoScalingGroupName": candidate.target_id,
            "MinSize": 0,
            "DesiredCapacity": 0,
        }

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        # MaxSize is carried through unchanged: the clamp does not touch it,
        # so the state after the clamp still has the group's own maximum, and
        # recording zero here would make every release look like a conflict.
        return {
            "min_size": 0,
            "max_size": int(candidate.state.get("max_size") or 0),
            "desired_capacity": 0,
        }

    def release_params(self, entry: Entry) -> dict[str, Any]:
        minimum = require_int(entry, "min_size")
        maximum = require_int(entry, "max_size")
        desired = require_int(entry, "desired_capacity")
        if desired > maximum or minimum > maximum:
            # A ledger entry is untrusted input — see the threat model. AWS
            # would refuse this too, but refusing it here means the report
            # says "the recorded sizes are inconsistent" rather than quoting
            # a ValidationError nobody can act on.
            raise ValueError(
                f"ledger entry {entry.clamp_id}/{entry.target.key} records "
                f"min={minimum}, desired={desired}, max={maximum}, which Auto "
                f"Scaling would reject. The entry is reported as unrestorable; "
                f"restore the group's sizes by hand and the ledger will settle on "
                f"the next reconcile."
            )
        return {
            "AutoScalingGroupName": entry.target.target_id,
            "MinSize": minimum,
            "MaxSize": maximum,
            "DesiredCapacity": desired,
        }
