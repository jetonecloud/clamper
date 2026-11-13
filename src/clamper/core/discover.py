"""Find every candidate for every enabled action, in every configured region.

Read calls only, through the handlers. What this module adds is the one rule
that discovery must never break: **a region or a resource type that could
not be read is reported, not skipped.** An account where `ecs:ListClusters`
is denied would otherwise look like an account with no ECS services, and a
plan that says "nothing to clamp" because it could not look is the
falsely-clean report the threat model calls a security issue.

Importable inside a Lambda.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actions import Action
from clamper.core.resources import Candidate, Unsupported, handler_for
from clamper.core.resources.sagemaker import endpoints
from clamper.core.session import Clients


@dataclass(frozen=True)
class DiscoveryError:
    action_id: str
    region: str
    message: str

    @property
    def label(self) -> str:
        return f"{self.action_id} in {self.region}: {self.message}"


@dataclass(frozen=True)
class Discovery:
    candidates: tuple[Candidate, ...] = ()
    unsupported: tuple[Unsupported, ...] = ()
    errors: tuple[DiscoveryError, ...] = field(default_factory=tuple)
    regions: tuple[str, ...] = ()


def discover(
    clients: Clients,
    actions: Iterable[Action],
    regions: Iterable[str],
    *,
    include_unsupported: bool = False,
) -> Discovery:
    """Every candidate, plus the errors that stop this being every candidate.

    `include_unsupported` adds SageMaker endpoints, which `clamper plan`
    shows and the enforcer has no use for — and no permission to read.
    """
    region_list = tuple(regions)
    action_list = tuple(actions)
    candidates: list[Candidate] = []
    unsupported: list[Unsupported] = []
    errors: list[DiscoveryError] = []
    for region in region_list:
        for action in action_list:
            try:
                candidates.extend(handler_for(action).discover(clients, region))
            except (ClientError, BotoCoreError) as exc:
                errors.append(DiscoveryError(action.id, region, _describe(exc)))
        if include_unsupported:
            try:
                unsupported.extend(endpoints(clients, region))
            except (ClientError, BotoCoreError) as exc:
                errors.append(DiscoveryError("sagemaker-endpoint", region, _describe(exc)))
    return Discovery(
        candidates=tuple(candidates),
        unsupported=tuple(unsupported),
        errors=tuple(errors),
        regions=region_list,
    )


def _describe(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        return f"{error.get('Code') or 'ClientError'} - {error.get('Message') or ''}".strip(" -")
    return str(exc)
