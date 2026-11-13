"""SageMaker: notebooks round-trip, endpoints are seen and never touched."""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import a_target, discover_one, handler, round_trip
from support import ACCOUNT, REGION

from clamper.core.ledger import STATUS_CLAMPED, STATUS_RELEASED, Entry
from clamper.core.resources.base import Unsupported
from clamper.core.resources.sagemaker import ENDPOINT_REASON, endpoints

ACTION = "sagemaker-notebook"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/sagemaker-role"


def make_notebook(
    sagemaker: Any, name: str = "research", *, instance_type: str = "ml.p3.2xlarge"
) -> str:
    sagemaker.create_notebook_instance(
        NotebookInstanceName=name,
        InstanceType=instance_type,
        RoleArn=ROLE,
        Tags=[{"Key": "clamper:managed", "Value": "true"}],
    )
    return name


def test_an_in_service_notebook_is_a_candidate(clients_moto: Any, moto_sagemaker: Any) -> None:
    make_notebook(moto_sagemaker)
    candidate = discover_one(ACTION, clients_moto, REGION, "research")

    assert candidate.clampable is True
    assert candidate.state == {"status": "InService"}
    assert candidate.tags["clamper:managed"] == "true"
    assert candidate.hourly_usd == 3.825


def test_a_stopped_notebook_is_reported(clients_moto: Any, moto_sagemaker: Any) -> None:
    make_notebook(moto_sagemaker)
    moto_sagemaker.stop_notebook_instance(NotebookInstanceName="research")
    candidate = discover_one(ACTION, clients_moto, REGION, "research")
    assert candidate.clampable is False
    assert "already stopped" in (candidate.reason or "")


@pytest.mark.parametrize("status", ["Pending", "Updating", "Deleting", "Failed"])
def test_a_transitional_status_is_not_attempted(status: str) -> None:
    candidate = handler(ACTION)._candidate(  # type: ignore[attr-defined]
        {"NotebookInstanceStatus": status, "InstanceType": "ml.t3.medium"},
        "research",
        {},
        REGION,
    )
    assert candidate.clampable is False
    assert status in (candidate.reason or "")


def test_clamp_and_release_round_trip(clients_moto: Any, moto_sagemaker: Any) -> None:
    make_notebook(moto_sagemaker)
    trip = round_trip(ACTION, clients_moto, REGION, "research")

    assert trip.clamped.status == STATUS_CLAMPED
    assert trip.state_after_clamp == {"status": "Stopped"}
    assert trip.clamped.applied_state == trip.state_after_clamp
    assert trip.released.status == STATUS_RELEASED
    assert trip.state_after_release == {"status": "InService"}


def test_live_state_is_none_for_a_notebook_that_is_gone(clients_moto: Any) -> None:
    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=a_target(ACTION, "never-existed", REGION),
        status=STATUS_CLAMPED,
        previous_state={"status": "InService"},
        applied_state={"status": "Stopped"},
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:00+00:00",
    )
    assert handler(ACTION).live_state(clients_moto, entry) is None


# --- endpoints: visible, never clamped ----------------------------------


def make_endpoint(sagemaker: Any, *, instance_type: str = "ml.p3.8xlarge", count: int = 2) -> None:
    sagemaker.create_model(
        ModelName="ranker",
        ExecutionRoleArn=ROLE,
        PrimaryContainer={"Image": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/ranker:1"},
    )
    sagemaker.create_endpoint_config(
        EndpointConfigName="ranker-config",
        ProductionVariants=[
            {
                "VariantName": "main",
                "ModelName": "ranker",
                "InitialInstanceCount": count,
                "InstanceType": instance_type,
            }
        ],
    )
    sagemaker.create_endpoint(EndpointName="ranker", EndpointConfigName="ranker-config")


def test_an_endpoint_is_seen_and_priced(clients_moto: Any, moto_sagemaker: Any) -> None:
    """The biggest number on the page stays on the page, even though v1
    will not touch it."""
    make_endpoint(moto_sagemaker)
    found = endpoints(clients_moto, REGION)

    assert len(found) == 1
    endpoint = found[0]
    assert isinstance(endpoint, Unsupported)
    assert endpoint.resource_id == "ranker"
    assert endpoint.reason == ENDPOINT_REASON
    assert endpoint.hourly_usd == pytest.approx(2 * 14.688)
    assert "2x ml.p3.8xlarge" in endpoint.description


def test_an_unpriced_endpoint_has_no_figure(clients_moto: Any, moto_sagemaker: Any) -> None:
    make_endpoint(moto_sagemaker, instance_type="ml.inf1.24xlarge")
    assert endpoints(clients_moto, REGION)[0].price is None


def test_endpoints_are_not_an_action(clients_moto: Any) -> None:
    """There is no action id an endpoint could be clamped under."""
    from clamper.core.actions import load_registry

    assert not any("endpoint" in action_id for action_id in load_registry().ids())
