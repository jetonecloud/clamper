"""Redshift provisioned clusters: pause, and resume the ones Clamper paused.

Pausing is Redshift's own reversible stop: on-demand compute billing is
suspended while storage keeps billing. The cluster keeps its data, endpoint,
parameter group and snapshots, so a resume is a restore rather than a
rebuild. That is the whole reason this action exists: a forgotten
multi-node RA3 cluster costs thousands a month in node hours, and the pause
takes exactly that to zero.

AWS accepts a pause only from `available`. Anything mid-change (resizing,
modifying, rebooting) is reported as not clamped with its status; one already
`paused` or `pausing` is reported as already off. Anything else AWS refuses
at the call comes back as a failed clamp carrying AWS's own message.

**Redshift Serverless** is found separately and never clamped: a workgroup
has no pause. It bills per RPU-hour of use, and nothing Clamper could stop
would take that to zero short of deleting it. `serverless_workgroups` lists
them as `Unsupported` so `clamper plan` still shows them; there is no code
path from it to the actuator.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from clamper.core.cost import redshift_price
from clamper.core.ledger import Entry
from clamper.core.resources.base import (
    Candidate,
    Handler,
    Unsupported,
    int_or_none,
    paginate,
    register,
    tag_map,
)
from clamper.core.session import Clients

#: The only status a pause is accepted from.
PAUSABLE = "available"

#: Statuses that mean it is already off or on its way there.
ALREADY_PAUSED = frozenset({"paused", "pausing"})

#: Not-found error codes. A cluster that is gone is reported, never treated
#: as released.
NOT_FOUND = frozenset({"ClusterNotFound", "ClusterNotFoundFault"})

SERVERLESS_REASON = (
    "Redshift Serverless workgroups have no pause: they bill per RPU-hour of use, "
    "and only deleting one would stop that. See docs/actions.md."
)


def _not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "") in NOT_FOUND


@register
class RedshiftClusterHandler(Handler):
    action_id = "redshift-cluster"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        found: list[Candidate] = []
        for page in paginate(clients, "redshift", "describe_clusters", region):
            for cluster in page.get("Clusters", []):
                candidate = self._candidate(cluster, region)
                if candidate is not None:
                    found.append(candidate)
        return found

    def _candidate(self, cluster: Mapping[str, Any], region: str) -> Candidate | None:
        identifier = str(cluster.get("ClusterIdentifier") or "")
        if not identifier:
            return None

        tags = tag_map(cluster.get("Tags"))
        status = str(cluster.get("ClusterStatus") or "unknown")
        node_type = str(cluster.get("NodeType") or "unknown")
        nodes = int_or_none(cluster.get("NumberOfNodes")) or 0
        price = redshift_price(node_type, nodes)
        description = f"{nodes}x {node_type}, {status}"

        if status in ALREADY_PAUSED:
            return self.unclampable(
                target_id=identifier,
                region=region,
                description=description,
                tags=tags,
                reason=f"already {status}",
                price=price,
            )
        if status != PAUSABLE:
            return self.unclampable(
                target_id=identifier,
                region=region,
                description=description,
                tags=tags,
                reason=f"status is {status}; AWS only accepts a pause from available",
                price=price,
            )
        return self.candidate(
            target_id=identifier,
            region=region,
            description=description,
            tags=tags,
            state={"status": status},
            price=price,
        )

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("redshift", region=entry.target.region)
        try:
            response = client.describe_clusters(ClusterIdentifier=entry.target.target_id)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        for cluster in response.get("Clusters", []):
            return {"status": str(cluster.get("ClusterStatus") or "unknown")}
        return None

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {"ClusterIdentifier": candidate.target_id}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        return {"status": "paused"}

    def release_params(self, entry: Entry) -> dict[str, Any]:
        return {"ClusterIdentifier": entry.target.target_id}


def serverless_workgroups(clients: Clients, region: str) -> list[Unsupported]:
    """Every live Redshift Serverless workgroup in `region`, for a plan.

    Read-only, and returns `Unsupported` rather than `Candidate`, so there is
    no code path from here to a clamp. No price: a workgroup bills for the
    RPU-hours its queries use, and a figure from its base capacity would be a
    guess at usage presented as a rate. A failure to read is the caller's to
    report.
    """
    found: list[Unsupported] = []
    for page in paginate(clients, "redshift-serverless", "list_workgroups", region):
        for workgroup in page.get("workgroups", []):
            name = str(workgroup.get("workgroupName") or "")
            status = str(workgroup.get("status") or "unknown")
            if not name or status == "DELETING":
                continue
            base = int_or_none(workgroup.get("baseCapacity"))
            shape = f"base {base} RPU" if base is not None else "serverless"
            found.append(
                Unsupported(
                    kind="Redshift Serverless",
                    resource_id=name,
                    region=region,
                    description=f"workgroup, {shape}, {status}",
                    reason=SERVERLESS_REASON,
                    price=None,
                )
            )
    return found
