"""What exists, narrowed to what may be clamped — with a reason for every drop.

The order is fixed, and the order is part of the safety design:

1. **In scope.** The breached limit's scope: the whole account, or resources
   carrying the scope's cost allocation tag and value.
2. **Enabled type.** The action is in `targets.types`.
3. **Selected.** Tagged with the managed tag and value, or named in a
   selector, or `include_untagged` is true with its acknowledgement.
4. **Not protected.** The protect tag, any value. Overridable by nothing:
   there is no config key and no `--force` that reaches past this line.
5. **Not Clamper's own.** Three independent checks, any one of which
   excludes. An enforcer that set its own reserved concurrency to zero could
   not release itself, and nothing else could either.
6. **Clampable now.** The handler's own verdict (already stopped, a read
   replica, ...) and the action's identifier shape — a resource whose id
   Clamper could not later validate for a release is never clamped.
7. **Under the cap.** `max_actions_per_run`. Applied last, so self-exclusion
   has always happened first and a run over the cap cannot have excluded the
   enforcer "later".

**Over the cap, nothing is clamped.** Not the first fifty: a plan far bigger
than the account's normal shape is more likely a mis-applied tag or a bug
than a real need, and truncating would act on whichever fifty the APIs
happened to return first. The selection is returned whole with `over_cap`
set, and the caller stops and says so.

A resource that was never opted in (step 3) is counted rather than listed.
Listing every untagged resource in an account would bury the twelve that
matter. Everything that *was* opted in and then dropped carries a reason and
becomes a line under NOT CLAMPED, because a resource that was not clamped is
never silently absent.

Importable inside a Lambda.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actions import Registry, load_registry
from clamper.core.config import Config, Limit
from clamper.core.names import StackNames
from clamper.core.resources import Candidate
from clamper.core.resources.base import STACK_TAG, paginate
from clamper.core.session import Clients

#: Why a candidate was dropped. Each is a category in every report.
DROP_OUT_OF_SCOPE = "out-of-scope"
DROP_TYPE_DISABLED = "type-disabled"
DROP_PROTECTED = "protected"
DROP_SELF = "self"
DROP_UNCLAMPABLE = "unclampable"

#: The three self-exclusion checks, named so a test can switch two off and
#: prove the third excludes the enforcer on its own.
CHECK_STACK_TAG = "stack-tag"
CHECK_STACK_RESOURCES = "stack-resources"
CHECK_NAME = "name"
SELF_CHECKS = (CHECK_STACK_TAG, CHECK_STACK_RESOURCES, CHECK_NAME)

#: Environment variables the stack sets on the enforcer, so it can recognise
#: its own resources without `cloudformation:*` permission — which the role
#: deliberately does not have.
ENV_STACK_NAME = "CLAMPER_STACK_NAME"
ENV_STACK_RESOURCES = "CLAMPER_STACK_RESOURCES"


@dataclass(frozen=True)
class SelfIdentity:
    """What Clamper's own stack is made of, for self-exclusion.

    `resource_ids` are the stack's physical ids and ARNs: from the stack's
    resource list when the CLI can read it, from the environment inside the
    enforcer. `names` are derived from the stack name and are the check that
    still works when both of the others have failed.
    """

    stack_name: str
    resource_ids: frozenset[str] = frozenset()
    checks: tuple[str, ...] = SELF_CHECKS
    #: Set when the stack's resource list could not be read, so check 2 has
    #: nothing to compare against. Reported, never treated as "no resources".
    read_error: str | None = None

    @property
    def names(self) -> frozenset[str]:
        return StackNames(self.stack_name).all

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SelfIdentity:
        env = os.environ if environ is None else environ
        stack = env.get(ENV_STACK_NAME, "").strip()
        if not stack:
            raise RuntimeError(
                f"{ENV_STACK_NAME} is not set. The enforcer needs its own stack name to "
                f"exclude its own resources, and it does not guess one."
            )
        ids = frozenset(
            item.strip() for item in env.get(ENV_STACK_RESOURCES, "").split(",") if item.strip()
        )
        return cls(stack_name=stack, resource_ids=ids)

    @classmethod
    def from_stack(cls, clients: Clients, stack_name: str) -> SelfIdentity:
        """The identity as the CLI sees it: the stack's own resource list.

        Read with the operator's credentials, which may read the stack. A
        stack that does not exist yet contributes no ids, which is not a
        problem: there is nothing of Clamper's to clamp yet. A list that could
        not be read is different, and `read_error` says so, so the CLI can
        tell somebody that one of the three checks is not in force. The
        stack-tag and by-name checks still hold either way.
        """
        ids: set[str] = set()
        try:
            for page in paginate(
                clients,
                "cloudformation",
                "list_stack_resources",
                clients.default_region or "",
                StackName=stack_name,
            ):
                for item in page.get("StackResourceSummaries", []):
                    physical = str(item.get("PhysicalResourceId") or "")
                    if physical:
                        ids.add(physical)
        except ClientError as exc:
            error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
            message = str(error.get("Message") or "")
            if error.get("Code") == "ValidationError" and "does not exist" in message:
                return cls(stack_name=stack_name)
            return cls(
                stack_name=stack_name,
                read_error=f"{error.get('Code') or 'ClientError'} - {message}".strip(" -"),
            )
        except BotoCoreError as exc:
            return cls(stack_name=stack_name, read_error=str(exc))
        return cls(stack_name=stack_name, resource_ids=frozenset(ids))

    def excludes(self, candidate: Candidate) -> str | None:
        """Why this candidate is Clamper's own, or `None` if it is not."""
        if CHECK_STACK_TAG in self.checks and candidate.tags.get(STACK_TAG) == self.stack_name:
            return f"part of Clamper's own stack {self.stack_name} (stack tag)"
        if CHECK_STACK_RESOURCES in self.checks and self._in_stack(candidate.target_id):
            return f"part of Clamper's own stack {self.stack_name} (stack resource list)"
        if CHECK_NAME in self.checks and candidate.target_id in self.names:
            return f"part of Clamper's own stack {self.stack_name} (by name)"
        return None

    def _in_stack(self, target_id: str) -> bool:
        if target_id in self.resource_ids:
            return True
        # Physical ids are sometimes ARNs. Compare the resource part, so a
        # function listed by ARN still matches a candidate known by name.
        for item in self.resource_ids:
            if item.startswith("arn:") and item.rsplit(":", 1)[-1].rsplit("/", 1)[-1] == target_id:
                return True
        return False


@dataclass(frozen=True)
class Dropped:
    """A candidate that was opted in and will not be clamped, and why."""

    candidate: Candidate
    category: str
    reason: str


@dataclass(frozen=True)
class Selection:
    targets: tuple[Candidate, ...] = ()
    dropped: tuple[Dropped, ...] = ()
    #: Every candidate carrying the protect tag, opted in or not. `clamper
    #: plan` lists them every time, so a resource that silently stopped being
    #: protected shows up in the next plan.
    protected: tuple[Candidate, ...] = ()
    #: Found but never opted in. Counted, not listed.
    not_selected: int = 0
    cap: int = 0
    over_cap: bool = False
    limits: tuple[Limit, ...] = field(default_factory=tuple)

    @property
    def actionable(self) -> tuple[Candidate, ...]:
        """What may actually be clamped: nothing at all when over the cap."""
        return () if self.over_cap else self.targets


def select(
    candidates: Iterable[Candidate],
    config: Config,
    limits: Iterable[Limit],
    identity: SelfIdentity,
    *,
    registry: Registry | None = None,
) -> Selection:
    """Apply the seven steps, in order. Pure: no AWS, no ledger."""
    registry = registry if registry is not None else load_registry()
    chosen_limits = tuple(limits)
    enabled = set(config.targets.types)
    targets: list[Candidate] = []
    dropped: list[Dropped] = []
    protected: list[Candidate] = []
    not_selected = 0

    for candidate in candidates:
        is_protected = config.targets.protect_tag in candidate.tags
        if is_protected:
            protected.append(candidate)

        # 3 is checked before 1 and 2 for reporting only: a resource nobody
        # opted in is counted rather than listed, whatever its scope. The
        # decision is the same either way — every step has to pass.
        if not _selected(candidate, config):
            not_selected += 1
            continue

        # 1. In scope.
        if not _in_scope(candidate, chosen_limits):
            dropped.append(Dropped(candidate, DROP_OUT_OF_SCOPE, _scope_reason(chosen_limits)))
            continue

        # 2. Enabled type.
        if candidate.action_id not in enabled:
            dropped.append(
                Dropped(
                    candidate,
                    DROP_TYPE_DISABLED,
                    f"{candidate.action_id} is not in targets.types",
                )
            )
            continue

        # 4. Protected. Nothing overrides this.
        if is_protected:
            dropped.append(
                Dropped(
                    candidate,
                    DROP_PROTECTED,
                    f"carries {config.targets.protect_tag}; nothing overrides it",
                )
            )
            continue

        # 5. Clamper's own.
        if (why := identity.excludes(candidate)) is not None:
            dropped.append(Dropped(candidate, DROP_SELF, why))
            continue

        # 6. Clampable now.
        if not candidate.clampable:
            dropped.append(
                Dropped(candidate, DROP_UNCLAMPABLE, candidate.reason or "not clampable")
            )
            continue
        if not registry.get(candidate.action_id).valid_identifier(candidate.target_id):
            dropped.append(
                Dropped(
                    candidate,
                    DROP_UNCLAMPABLE,
                    "its id does not have the shape Clamper validates before a release, "
                    "so a clamp could not be undone through Clamper",
                )
            )
            continue

        targets.append(candidate)

    # 7. The cap, last.
    cap = config.safety.max_actions_per_run
    ordered = tuple(sorted(targets, key=_cost_order))
    return Selection(
        targets=ordered,
        dropped=tuple(dropped),
        protected=tuple(protected),
        not_selected=not_selected,
        cap=cap,
        over_cap=len(ordered) > cap,
        limits=chosen_limits,
    )


def _selected(candidate: Candidate, config: Config) -> bool:
    targets = config.targets
    if candidate.tags.get(targets.managed_tag) == targets.managed_tag_value:
        return True
    if candidate.target_id in targets.ids_for(candidate.action_id):
        return True
    return targets.include_untagged


def _in_scope(candidate: Candidate, limits: tuple[Limit, ...]) -> bool:
    """In scope of any of the limits being enforced.

    A whole-account limit covers everything. A tag-scoped one covers what
    carries that tag and value — on the resource, which is the closest
    Clamper can get to "the resources this budget's spend came from".
    """
    for limit in limits:
        scope = limit.scope
        if scope.tag is None:
            return True
        if candidate.tags.get(scope.tag) == scope.value:
            return True
    return False


def _scope_reason(limits: tuple[Limit, ...]) -> str:
    scopes = ", ".join(f"{limit.id} ({limit.scope.label})" for limit in limits) or "none"
    return f"not in the scope of the breached limit(s): {scopes}"


def _cost_order(candidate: Candidate) -> tuple[int, float, str, str]:
    """Most expensive first, unpriced after, then stable by type and id.

    So the first line of a plan is the line worth reading, and two runs over
    the same account list targets in the same order.
    """
    hourly = candidate.hourly_usd
    return (
        0 if hourly is not None else 1,
        -(hourly or 0.0),
        candidate.action_id,
        candidate.target_id,
    )
