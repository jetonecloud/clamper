"""SageMaker notebook instances: stop, and start the ones Clamper stopped.

And, separately, **endpoints**: found, priced, and listed under "not clamped"
with the reason, because they are the expensive SageMaker thing and v1 will
not touch them. The only way to stop an endpoint's cost is `DeleteEndpoint`,
and its inverse is `CreateEndpoint` with a config that may reference a model
that has since been deleted. That is a rebuild that might fail, not a
restore, and a release that silently fails to bring back a production
inference endpoint is worse than never having stopped it.
`UpdateEndpointWeightsAndCapacities` cannot go to zero instances.

So the endpoint reader is read-only and returns `Unsupported`, never a
`Candidate`. There is no code path from it to the actuator.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from clamper.core.cost import Price, sagemaker_price
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

STOPPABLE = "InService"
ALREADY_STOPPED = frozenset({"Stopped", "Stopping"})

ENDPOINT_REASON = (
    "SageMaker endpoints are not in v1: the only way to stop one's cost is to delete "
    "it, and the inverse is a rebuild that can fail. See docs/actions.md."
)


def _not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or "")
    # SageMaker reports a missing notebook as a ValidationException whose
    # message is "RecordNotFound", rather than with a not-found code.
    lowered = message.lower()
    return code == "ResourceNotFound" or (
        code == "ValidationException"
        and ("recordnotfound" in lowered or "does not exist" in lowered)
    )


@register
class SageMakerNotebookHandler(Handler):
    action_id = "sagemaker-notebook"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        client = clients.get("sagemaker", region=region)
        found: list[Candidate] = []
        for page in paginate(clients, "sagemaker", "list_notebook_instances", region):
            for notebook in page.get("NotebookInstances", []):
                name = str(notebook.get("NotebookInstanceName") or "")
                if not name:
                    continue
                arn = str(notebook.get("NotebookInstanceArn") or "")
                tags = self._tags(client, arn)
                found.append(self._candidate(notebook, name, tags, region))
        return found

    def _candidate(
        self, notebook: Mapping[str, Any], name: str, tags: Mapping[str, str], region: str
    ) -> Candidate:
        status = str(notebook.get("NotebookInstanceStatus") or "unknown")
        instance_type = str(notebook.get("InstanceType") or "unknown")
        price = sagemaker_price(instance_type)
        description = f"{instance_type}, {status}"

        if status in ALREADY_STOPPED:
            return self.unclampable(
                target_id=name,
                region=region,
                description=description,
                tags=tags,
                reason=f"already {status.lower()}",
                price=price,
            )
        if status != STOPPABLE:
            return self.unclampable(
                target_id=name,
                region=region,
                description=description,
                tags=tags,
                reason=f"status is {status}; AWS only accepts a stop from InService",
                price=price,
            )
        return self.candidate(
            target_id=name,
            region=region,
            description=description,
            tags=tags,
            state={"status": status},
            price=price,
        )

    def _tags(self, client: Any, arn: str) -> dict[str, str]:
        if not arn:
            return {}
        response = client.list_tags(ResourceArn=arn)
        return tag_map(response.get("Tags"))

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("sagemaker", region=entry.target.region)
        try:
            response = client.describe_notebook_instance(
                NotebookInstanceName=entry.target.target_id
            )
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        return {"status": str(response.get("NotebookInstanceStatus") or "unknown")}

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {"NotebookInstanceName": candidate.target_id}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        return {"status": "Stopped"}

    def release_params(self, entry: Entry) -> dict[str, Any]:
        return {"NotebookInstanceName": entry.target.target_id}


def endpoints(clients: Clients, region: str) -> list[Unsupported]:
    """Every in-service endpoint in `region`, priced where the table allows.

    Read-only, and returns `Unsupported` rather than `Candidate`, so there is
    no code path from here to a clamp. A failure to read is the caller's to
    report: an endpoint list that silently came back empty would hide the
    biggest number in the account.
    """
    client = clients.get("sagemaker", region=region)
    found: list[Unsupported] = []
    for page in paginate(clients, "sagemaker", "list_endpoints", region):
        for summary in page.get("Endpoints", []):
            name = str(summary.get("EndpointName") or "")
            status = str(summary.get("EndpointStatus") or "unknown")
            if not name or status in {"Deleting", "Failed"}:
                continue
            described = client.describe_endpoint(EndpointName=name)
            price, shape = _endpoint_price(client, described)
            found.append(
                Unsupported(
                    kind="SageMaker endpoint",
                    resource_id=name,
                    region=region,
                    description=f"{shape}, {status}" if shape else status,
                    reason=ENDPOINT_REASON,
                    price=price,
                )
            )
    return found


def _endpoint_price(client: Any, endpoint: Mapping[str, Any]) -> tuple[Price | None, str]:
    """Sum the variants' instance-hours, or `None` if any is unpriced.

    The same rule as an Auto Scaling group: a partial sum understates the
    figure whose whole job is to put the expensive thing first.
    """
    config_name = str(endpoint.get("EndpointConfigName") or "")
    if not config_name:
        return None, ""
    config = client.describe_endpoint_config(EndpointConfigName=config_name)
    counts: dict[str, int] = {}
    for variant in endpoint.get("ProductionVariants") or []:
        name = str(variant.get("VariantName") or "")
        count = int_or_none(variant.get("CurrentInstanceCount")) or 0
        counts[name] = count
    total = 0.0
    shape: list[str] = []
    priced = True
    for variant in config.get("ProductionVariants") or []:
        instance_type = str(variant.get("InstanceType") or "")
        if not instance_type:
            # Serverless inference: billed per request, no instance-hours.
            shape.append("serverless")
            priced = False
            continue
        name = str(variant.get("VariantName") or "")
        count = counts.get(name, int_or_none(variant.get("InitialInstanceCount")) or 0)
        shape.append(f"{count}x {instance_type}")
        unit = sagemaker_price(instance_type)
        if unit is None:
            priced = False
            continue
        total += unit.hourly_usd * count
    if not priced or not shape:
        return None, ", ".join(shape)
    return (
        Price(
            hourly_usd=round(total, 6),
            basis=f"{', '.join(shape)}, us-east-1 on-demand list price",
        ),
        ", ".join(shape),
    )
