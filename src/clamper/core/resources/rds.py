"""RDS: DB instances and Aurora clusters. Stop, and start the ones Clamper stopped.

Two actions in one module because they share a client, a tag shape and the
one fact about RDS that shapes everything else:

> "If you don't manually start your DB instance after it is stopped for seven
> consecutive days, RDS automatically starts your DB instance for you."

So a clamp on RDS has a seven-day shelf life. Both rows in `actions.yml`
carry `restart_after_days: 7`, and the reconcile run reads `live_state` for
every entry still `clamped`, re-stops anything AWS started, and **says so in
the notification**. A database silently coming back up a week into a cost
incident is exactly the kind of thing a person needs told rather than
handled.

What AWS refuses, and Clamper therefore reports as not clamped with the
reason rather than attempting:

- an instance that has a read replica, or is one;
- an RDS for SQL Server instance in a Multi-AZ deployment;
- an instance that belongs to an Aurora cluster — the cluster is the thing to
  stop, and stopping a member directly is refused by AWS anyway;
- an Aurora Serverless v1 cluster, which pauses itself and cannot be stopped.

A cluster in a global database cannot be stopped either. There is no cheap
way to tell from `DescribeDBClusters` alone, so that one is left to AWS to
refuse, and comes back as a failed clamp carrying AWS's own message.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from clamper.core.cost import rds_price
from clamper.core.ledger import Entry
from clamper.core.resources.base import (
    Candidate,
    Handler,
    paginate,
    register,
    tag_map,
)
from clamper.core.session import Clients

#: The only status a stop is accepted from.
STOPPABLE = "available"

#: Statuses that mean it is already off or on its way there.
ALREADY_STOPPED = frozenset({"stopped", "stopping"})

#: Not-found error codes. A resource that is gone is reported, never treated
#: as released.
NOT_FOUND = frozenset({"DBInstanceNotFound", "DBInstanceNotFoundFault", "DBClusterNotFoundFault"})


def _not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "") in NOT_FOUND


@register
class RdsInstanceHandler(Handler):
    action_id = "rds-instance"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        found: list[Candidate] = []
        for page in paginate(clients, "rds", "describe_db_instances", region):
            for instance in page.get("DBInstances", []):
                candidate = self._candidate(instance, region)
                if candidate is not None:
                    found.append(candidate)
        return found

    def _candidate(self, instance: Mapping[str, Any], region: str) -> Candidate | None:
        identifier = str(instance.get("DBInstanceIdentifier") or "")
        if not identifier:
            return None

        tags = tag_map(instance.get("TagList"))
        status = str(instance.get("DBInstanceStatus") or "unknown")
        instance_class = str(instance.get("DBInstanceClass") or "unknown")
        engine = str(instance.get("Engine") or "unknown")
        multi_az = bool(instance.get("MultiAZ"))
        price = rds_price(instance_class, multi_az=multi_az)
        description = f"{instance_class}, {engine}{', Multi-AZ' if multi_az else ''}, {status}"

        def refuse(reason: str) -> Candidate:
            return self.unclampable(
                target_id=identifier,
                region=region,
                description=description,
                tags=tags,
                reason=reason,
                price=price,
            )

        if cluster := instance.get("DBClusterIdentifier"):
            return refuse(
                f"member of the Aurora cluster {cluster}. Clamp the cluster instead - "
                f"tag it clamper:managed and the aurora-cluster action stops it."
            )
        if instance.get("ReadReplicaSourceDBInstanceIdentifier"):
            return refuse("is a read replica; AWS refuses to stop one")
        if instance.get("ReadReplicaDBInstanceIdentifiers"):
            return refuse("has a read replica; AWS refuses to stop it")
        if engine.startswith("sqlserver") and multi_az:
            return refuse("RDS for SQL Server in a Multi-AZ deployment; AWS refuses to stop it")
        if status in ALREADY_STOPPED:
            return refuse(f"already {status}")
        if status != STOPPABLE:
            return refuse(f"status is {status}; AWS only accepts a stop from available")

        return self.candidate(
            target_id=identifier,
            region=region,
            description=description,
            tags=tags,
            state={"status": status},
            price=price,
        )

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("rds", region=entry.target.region)
        try:
            response = client.describe_db_instances(DBInstanceIdentifier=entry.target.target_id)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        for instance in response.get("DBInstances", []):
            return {"status": str(instance.get("DBInstanceStatus") or "unknown")}
        return None

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {"DBInstanceIdentifier": candidate.target_id}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        # `stopped`, not `stopping`, for the same reason as EC2: a release
        # compares the live state with this, and by then a successful stop
        # has finished.
        return {"status": "stopped"}

    def release_params(self, entry: Entry) -> dict[str, Any]:
        return {"DBInstanceIdentifier": entry.target.target_id}
