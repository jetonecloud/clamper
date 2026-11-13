"""What a resource handler is, and the registry of them.

One handler per row in `actions.yml`. A handler knows three things and
nothing else:

1. **How to find candidates** — the `Describe`/`Get`/`List` calls, the tags,
   and whether this particular resource can be clamped right now.
2. **What to record** — the fields `actions.yml` declares under `records`,
   read off the live resource.
3. **How to build the parameters** for a clamp or a release.

What a handler deliberately cannot do:

- **Name a boto3 method.** It returns parameters; the actuator resolves the
  method from the registry. `tests/test_safety.py` asserts no method name
  exists in the Python source at all.
- **Decide whether to clamp.** `core/targets.py` applies scope, selection,
  protection, self-exclusion and the per-run cap. A handler that made its own
  decision would be a second policy engine, and the two would disagree.
- **Act.** A handler makes read calls only, and the structural test enforces
  that by service.

`clampable=False` is never a silent drop. It always carries a `reason`, and
`core/targets.py` turns it into a ledger skip and a line under NOT CLAMPED.
A target Clamper could not stop that nobody can explain is a target nobody
trusts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clamper.core.actions import PHASE_RELEASE, Action
from clamper.core.cost import Price
from clamper.core.ledger import Entry, Target
from clamper.core.session import Clients

#: The tag Auto Scaling puts on every instance it launches. An instance
#: carrying it is skipped by the EC2 handler and dealt with by the Auto
#: Scaling handler: AWS relaunches instances a budget action stops, so
#: stopping one directly looks like it worked and quietly does not.
ASG_TAG = "aws:autoscaling:groupName"

#: The tag CloudFormation puts on everything in a stack. The first and
#: cheapest of the three self-exclusion checks in `core/targets.py`.
STACK_TAG = "aws:cloudformation:stack-name"


@dataclass(frozen=True)
class Candidate:
    """A discovered resource, before anything has decided about it.

    `state` is the snapshot: exactly the fields `actions.yml` declares under
    `records`, and nothing else. A candidate carries no attribute that is not
    needed to clamp it, release it, or explain it, because everything here can
    end up in the ledger or in a report.
    """

    action_id: str
    target_id: str
    region: str
    title: str
    #: One short phrase for a report: the instance type, the engine, the
    #: current size. Never a tag value — tags are free text and people put
    #: things in them.
    description: str
    tags: Mapping[str, str]
    #: The previous state, to be recorded before the clamp.
    state: Mapping[str, Any]
    clampable: bool = True
    #: Why not, when `clampable` is false. Always set when it is.
    reason: str | None = None
    price: Price | None = None
    #: Anything the handler needs to build a call that is not the identifier
    #: — the ECS cluster ARN, for instance. Carried into the ledger entry so
    #: a release months later does not need to rediscover it.
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.clampable and not self.reason:
            raise ValueError(
                f"{self.action_id} {self.target_id} is not clampable and carries no "
                f"reason. Every skip has to be explainable: it becomes a line under "
                f"NOT CLAMPED and a ledger entry."
            )

    @property
    def target(self) -> Target:
        return Target(
            action_id=self.action_id,
            target_id=self.target_id,
            region=self.region,
            title=self.title,
            description=self.description,
        )

    @property
    def hourly_usd(self) -> float | None:
        return self.price.hourly_usd if self.price else None


class Handler(ABC):
    """Base class for the per-resource-type handlers."""

    #: Must match a row in `actions.yml`. Checked at registration.
    action_id: str

    def __init__(self, action: Action) -> None:
        self.action = action

    # --- discovery ------------------------------------------------------

    @abstractmethod
    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        """Every resource of this type in `region`, with its current state.

        Read calls only. Pagination is mandatory, not optional: an account
        with 1,200 instances that silently reported the first 1,000 would
        clamp a subset and report it as everything.
        """

    @abstractmethod
    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        """The resource's current state, for one ledger entry.

        Used twice, and both uses matter:

        - **Conflict detection on release.** Compared with `applied_state` to
          tell "somebody changed this by hand" from "the clamp worked".
        - **The re-stop.** RDS restarts a stopped instance after seven days,
          so the reconcile run reads this and re-applies the clamp.

        `None` means the resource is gone. That is reported, not treated as
        released: a deleted resource is not a restored one.
        """

    # --- building calls -------------------------------------------------

    @abstractmethod
    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        """The parameters for the clamp call.

        Must be exactly the action's declared `params`. The actuator checks
        that, so an omission is a loud failure rather than a partial update.
        """

    @abstractmethod
    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        """What the resource will look like once the clamp has been applied.

        Recorded alongside the previous state, and the whole basis of
        conflict detection. Without it, "the instance is running" could mean
        "somebody started it" or "the clamp failed".
        """

    @abstractmethod
    def release_params(self, entry: Entry) -> dict[str, Any]:
        """The parameters to put the resource back, from the ledger entry.

        Everything comes from `entry.previous_state`. A handler that
        rediscovered the resource to build this would restore it to whatever
        it looks like now, which is not a release.
        """

    def release_phase(self, entry: Entry) -> str:
        """Which inverse to use.

        Only Lambda overrides this: a function that had no reserved
        concurrency before the clamp is restored by removing the setting
        rather than by setting a number nobody chose.
        """
        return PHASE_RELEASE

    # --- helpers for subclasses -----------------------------------------

    def candidate(
        self,
        *,
        target_id: str,
        region: str,
        description: str,
        tags: Mapping[str, str],
        state: Mapping[str, Any],
        clampable: bool = True,
        reason: str | None = None,
        price: Price | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> Candidate:
        """A `Candidate` with this handler's identity filled in.

        Also checks that `state` is exactly what `actions.yml` declares under
        `records`. A handler that recorded four fields where the declaration
        promised three would produce a ledger entry nothing validates, and a
        handler that recorded two would produce an unreleasable clamp.
        """
        if clampable:
            declared = set(self.action.records)
            given = set(state)
            if given != declared:
                raise ValueError(
                    f"{self.action_id} recorded {sorted(given)} but actions.yml "
                    f"declares records: {sorted(declared)}. A mismatch here is an "
                    f"unreleasable clamp."
                )
        return Candidate(
            action_id=self.action_id,
            target_id=target_id,
            region=region,
            title=self.action.title,
            description=description,
            tags=dict(tags),
            state=dict(state),
            clampable=clampable,
            reason=reason,
            price=price,
            extra=dict(extra or {}),
        )

    def unclampable(
        self,
        *,
        target_id: str,
        region: str,
        description: str,
        tags: Mapping[str, str],
        reason: str,
        price: Price | None = None,
    ) -> Candidate:
        """A candidate Clamper has found and cannot stop, with the reason.

        A separate constructor because it skips the `records` check: there is
        no state worth recording for something that will not be touched, and
        requiring one would mean inventing fields.
        """
        return Candidate(
            action_id=self.action_id,
            target_id=target_id,
            region=region,
            title=self.action.title,
            description=description,
            tags=dict(tags),
            state={},
            clampable=False,
            reason=reason,
            price=price,
        )


# --- the registry -------------------------------------------------------

_HANDLERS: dict[str, type[Handler]] = {}


def register(handler: type[Handler]) -> type[Handler]:
    """Register a handler class. Used as a decorator."""
    action_id = getattr(handler, "action_id", "")
    if not action_id:
        raise ValueError(f"{handler.__name__} has no action_id.")
    if action_id in _HANDLERS:
        raise ValueError(f"two handlers claim {action_id!r}: {handler.__name__} is the second.")
    _HANDLERS[action_id] = handler
    return handler


def handler_for(action: Action) -> Handler:
    """The handler for one action.

    A declared action with no handler is an error rather than an action that
    quietly finds nothing — which would present to a user as "no targets
    matched" and be indistinguishable from a healthy account.
    """
    try:
        cls = _HANDLERS[action.id]
    except KeyError:
        raise KeyError(
            f"{action.id} is declared in actions.yml but no handler is registered "
            f"for it. An action with no handler would silently find nothing, which "
            f"looks exactly like an account with nothing to clamp."
        ) from None
    return cls(action)


def handlers_for(actions: Iterable[Action]) -> list[Handler]:
    return [handler_for(action) for action in actions]


def registered_ids() -> frozenset[str]:
    return frozenset(_HANDLERS)


# --- shared AWS-shape helpers -------------------------------------------


def tag_map(
    raw: Sequence[Mapping[str, Any]] | None, *, key: str = "Key", value: str = "Value"
) -> dict[str, str]:
    """AWS's list-of-pairs tag shape as a dict.

    Every service spells it differently — `Tags` with `Key`/`Value`, `TagList`
    with the same, `tags` with `key`/`value` — so the field names are
    arguments rather than assumed.
    """
    found: dict[str, str] = {}
    for item in raw or ():
        name = item.get(key)
        if name is None:
            continue
        found[str(name)] = str(item.get(value, "") or "")
    return found


def paginate(clients: Clients, service: str, operation: str, region: str, **kwargs: Any) -> Any:
    """Every page of a paginated read.

    A thin wrapper so no handler forgets. An account with 1,200 instances
    that silently reported the first 1,000 would clamp a subset and report it
    as everything, which is the misreport this tool must never make.
    """
    client = clients.get(service, region=region)
    paginator = client.get_paginator(operation)
    return paginator.paginate(**kwargs)


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def require_int(entry: Entry, field_name: str, *, minimum: int = 0) -> int:
    """A recorded integer, validated before it is put in a call.

    A ledger entry is untrusted input: the threat model covers someone with
    write access to the table putting `999999` in a `desired_capacity`. A
    release builds a real AWS call from these values, so they are checked
    rather than passed through.
    """
    value = int_or_none(entry.previous_state.get(field_name))
    if value is None or value < minimum:
        raise ValueError(
            f"ledger entry {entry.clamp_id}/{entry.target.key} has "
            f"{field_name}={entry.previous_state.get(field_name)!r}, which is not a "
            f"whole number of at least {minimum}. Clamper will not build an AWS call "
            f"from it — the entry is reported as unrestorable instead."
        )
    return value
