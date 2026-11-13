"""Discovery: a region or type that could not be read is reported, not skipped."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from support import OTHER_REGION, REGION
from test_resource_ec2 import launch

from clamper.core.actions import load_registry
from clamper.core.discover import discover
from clamper.core.resources import base


def test_every_region_and_type_is_searched(clients_moto: Any, moto_ec2: Any) -> None:
    launch(moto_ec2)
    found = discover(clients_moto, load_registry().actions, [REGION, OTHER_REGION])
    assert found.errors == ()
    assert found.regions == (REGION, OTHER_REGION)
    assert [item.region for item in found.candidates if item.action_id == "ec2-instance"] == [
        REGION
    ]


def test_a_denied_read_is_an_error_not_an_empty_result(
    clients_moto: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account where ecs:ListClusters is denied would otherwise look like
    an account with no ECS services — a falsely-clean plan."""

    def denied(self: Any, clients: Any, region: str) -> list[Any]:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}},
            "ListClusters",
        )

    handler_class = base._HANDLERS["ecs-service"]
    monkeypatch.setattr(handler_class, "discover", denied)

    found = discover(clients_moto, load_registry().actions, [REGION])
    assert [(item.action_id, item.region) for item in found.errors] == [("ecs-service", REGION)]
    assert "AccessDeniedException" in found.errors[0].message


def test_endpoints_are_only_read_when_asked(clients_moto: Any) -> None:
    """The enforcer has no permission to list endpoints and no use for them."""
    found = discover(clients_moto, (), [REGION])
    assert found.unsupported == () and found.errors == ()
    found = discover(clients_moto, (), [REGION], include_unsupported=True)
    assert found.errors == ()
