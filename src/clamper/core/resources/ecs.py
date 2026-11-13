"""ECS services: desired count to zero, and restore the recorded count.

An ECS service's identity is a *pair* — the cluster and the service — so the
target id is `<cluster>/<service>`. That keeps everything downstream simple:
the ledger entry carries the whole identity in the field it already has, and
a release months later needs no rediscovery to work out which cluster the
service was in. The alternative was a side-channel field on the candidate,
carried through the ledger, that only this one handler used.

The honest cost, in `actions.yml` notes and in every plan: running tasks
stop, so a service behind a load balancer starts failing health checks and
its target group empties. And the clamp is recorded as soon as the API
accepts it, which is **not** the same as the tasks being gone — a service
with a long draining timeout keeps billing for a few minutes after Clamper
has reported it stopped.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

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

#: `DescribeServices` takes at most ten service ARNs per call.
DESCRIBE_BATCH = 10

#: The separator in the target id. A forward slash, because that is already
#: how an ECS service ARN spells the same pair and it cannot appear in either
#: a cluster name or a service name.
SEPARATOR = "/"


def split_target(target_id: str) -> tuple[str, str]:
    """`<cluster>/<service>` back into its two halves.

    Raises rather than guessing. This value reaches a real AWS call, and a
    ledger entry is untrusted input.
    """
    cluster, separator, service = target_id.partition(SEPARATOR)
    if not separator or not cluster or not service or SEPARATOR in service:
        raise ValueError(
            f"{target_id!r} is not an ECS target id. They are "
            f"`<cluster>/<service>`, and both halves are required."
        )
    return cluster, service


@register
class EcsServiceHandler(Handler):
    action_id = "ecs-service"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        client = clients.get("ecs", region=region)
        found: list[Candidate] = []
        for cluster_arn in self._clusters(clients, region):
            cluster_name = cluster_arn.rsplit(SEPARATOR, 1)[-1]
            for batch in self._service_batches(clients, region, cluster_arn):
                described = client.describe_services(cluster=cluster_arn, services=batch)
                for service in described.get("services", []):
                    candidate = self._candidate(client, service, cluster_name, region)
                    if candidate is not None:
                        found.append(candidate)
        return found

    def _clusters(self, clients: Clients, region: str) -> list[str]:
        arns: list[str] = []
        for page in paginate(clients, "ecs", "list_clusters", region):
            arns.extend(str(item) for item in page.get("clusterArns", []))
        return arns

    def _service_batches(
        self, clients: Clients, region: str, cluster_arn: str
    ) -> Iterator[list[str]]:
        """Service ARNs in batches of ten, because that is the API's limit.

        A cluster with eleven services and no batching would describe the
        first ten and silently miss one, which would present as a service
        Clamper never mentioned rather than as an error.
        """
        batch: list[str] = []
        for page in paginate(clients, "ecs", "list_services", region, cluster=cluster_arn):
            for arn in page.get("serviceArns", []):
                batch.append(str(arn))
                if len(batch) == DESCRIBE_BATCH:
                    yield batch
                    batch = []
        if batch:
            yield batch

    def _candidate(
        self, client: Any, service: Mapping[str, Any], cluster_name: str, region: str
    ) -> Candidate | None:
        name = str(service.get("serviceName") or "")
        if not name:
            return None
        target_id = f"{cluster_name}{SEPARATOR}{name}"

        arn = str(service.get("serviceArn") or "")
        tags = self._tags(client, arn)
        desired = int_or_none(service.get("desiredCount")) or 0
        running = int_or_none(service.get("runningCount")) or 0
        status = str(service.get("status") or "unknown")
        description = f"desired {desired}, running {running}"

        # ECS has no per-task price Clamper can defend: the cost depends on
        # the task definition's CPU and memory reservations, the launch type,
        # and for EC2 launch type on instances the service does not own. So
        # no figure, rather than a wrong one.
        if status != "ACTIVE":
            return self.unclampable(
                target_id=target_id,
                region=region,
                description=f"{description}, {status}",
                tags=tags,
                reason=f"service status is {status}, not ACTIVE",
            )

        if desired == 0:
            return self.unclampable(
                target_id=target_id,
                region=region,
                description=description,
                tags=tags,
                reason="desired count is already zero",
            )

        return self.candidate(
            target_id=target_id,
            region=region,
            description=description,
            tags=tags,
            state={"desired_count": desired},
        )

    def _tags(self, client: Any, arn: str) -> dict[str, str]:
        """A service's tags.

        A separate call per service rather than `include=["TAGS"]` on
        `DescribeServices`. The include form would save a call, but the
        permission it needs is not the one `actions.yml` declares, and an
        undeclared permission is either a policy that is wider than the
        declaration or a call that fails in production and works in a test.
        """
        if not arn:
            return {}
        response = client.list_tags_for_resource(resourceArn=arn)
        return tag_map(response.get("tags"), key="key", value="value")

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        cluster, service = split_target(entry.target.target_id)
        client = clients.get("ecs", region=entry.target.region)
        response = client.describe_services(cluster=cluster, services=[service])
        for found in response.get("services", []):
            if str(found.get("status") or "") == "INACTIVE":
                # A deleted service. Reported, not treated as released: a
                # service that is gone is not a service that was restored.
                return None
            return {"desired_count": int_or_none(found.get("desiredCount")) or 0}
        return None

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        cluster, service = split_target(candidate.target_id)
        return {"cluster": cluster, "service": service, "desiredCount": 0}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        return {"desired_count": 0}

    def release_params(self, entry: Entry) -> dict[str, Any]:
        cluster, service = split_target(entry.target.target_id)
        return {
            "cluster": cluster,
            "service": service,
            # Validated, not passed through: a ledger entry is untrusted
            # input and this number becomes a real task count.
            "desiredCount": require_int(entry, "desired_count", minimum=1),
        }
