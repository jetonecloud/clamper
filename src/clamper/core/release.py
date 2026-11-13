"""Putting things back: the inverse of a clamp, from the ledger.

`clamper release <clamp-id>` and `clamper release --all` both end up here.
Everything a release needs comes from the ledger entry — never from
rediscovering the resource, because a release built from what the resource
looks like *now* restores it to what it looks like now, which is not a
release.

The order of checks for one entry, and why each is there:

1. **It is active.** Only a `clamped` entry has something to restore. A
   `failed` or `dry-run` entry never stopped anything, and releasing one
   would start a resource Clamper never stopped.
2. **Its identifiers have the declared shape.** A ledger entry is untrusted
   input — the threat model covers somebody with write access to the table
   — so the target id is checked against the action's `identifier_pattern`
   and the region against the region shape before any call is built.
3. **The resource still exists.** A deleted resource is reported as gone and
   the entry is closed as `release-failed`. Never as released: a resource
   that no longer exists is not one that was put back.
4. **Nobody changed it since.** The live state is compared with
   `applied_state`, what Clamper set it to. If they differ, somebody has
   touched it by hand since the clamp, and the entry is reported as a
   **conflict** and left alone. `--force` restores anyway and the outcome
   says that it overrode a conflict.
5. **The recorded values are sane.** The handler validates them as it builds
   the parameters; a value it refuses makes the entry unrestorable, with the
   reason, rather than reaching AWS.

Then the call goes through the actuator like every other write, and the
ledger records the outcome. A failed release stays visible as
`release-failed` rather than reverting to `clamped`.

Importable inside a Lambda.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actions import Registry, UnknownAction, load_registry
from clamper.core.actuator import Actuator, Call, Result
from clamper.core.config import REGION
from clamper.core.ledger import Entry, LedgerStore
from clamper.core.resources import handler_for
from clamper.core.session import Clients

OUTCOME_RELEASED = "released"
OUTCOME_WOULD_RELEASE = "would-release"
OUTCOME_FAILED = "release-failed"
OUTCOME_CONFLICT = "conflict"
OUTCOME_GONE = "gone"
OUTCOME_UNRESTORABLE = "unrestorable"
OUTCOME_NOT_ACTIVE = "not-active"
OUTCOME_ERROR = "error"

#: Outcomes that leave a resource still stopped and need a person. Any of
#: these makes the exit code 2.
NEEDS_ATTENTION = frozenset(
    {OUTCOME_FAILED, OUTCOME_CONFLICT, OUTCOME_GONE, OUTCOME_UNRESTORABLE, OUTCOME_ERROR}
)


@dataclass(frozen=True)
class ReleaseOutcome:
    """What happened to one ledger entry when somebody asked to release it."""

    entry: Entry
    outcome: str
    detail: str
    #: True when `--force` restored over a detected conflict. Always said
    #: out loud in every output, because it means a hand-made change was
    #: overwritten.
    forced: bool = False
    live_state: Mapping[str, Any] | None = None

    @property
    def released(self) -> bool:
        return self.outcome == OUTCOME_RELEASED

    @property
    def needs_attention(self) -> bool:
        return self.outcome in NEEDS_ATTENTION


def release(
    entries: Iterable[Entry],
    *,
    clients: Clients,
    ledger: LedgerStore,
    actuator: Actuator,
    force: bool = False,
    registry: Registry | None = None,
) -> tuple[ReleaseOutcome, ...]:
    """Release every entry, one at a time, and report each.

    One entry that cannot be released never stops the others: a release run
    is most often somebody trying to bring a whole system back, and stopping
    at the first awkward entry would leave it half-restored.
    """
    registry = registry if registry is not None else load_registry()
    return tuple(
        _release_one(
            entry,
            clients=clients,
            ledger=ledger,
            actuator=actuator,
            force=force,
            registry=registry,
        )
        for entry in entries
    )


def _release_one(
    entry: Entry,
    *,
    clients: Clients,
    ledger: LedgerStore,
    actuator: Actuator,
    force: bool,
    registry: Registry,
) -> ReleaseOutcome:
    # 1. Active.
    if not entry.active:
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_NOT_ACTIVE,
            detail=f"status is {entry.status}; only a clamped entry has something to restore",
        )

    # 2. Shape.
    try:
        action = registry.get(entry.target.action_id)
    except UnknownAction:
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_UNRESTORABLE,
            detail=(
                f"action {entry.target.action_id!r} is not declared in this version of "
                f"Clamper, so there is no release it is allowed to make"
            ),
        )
    if not action.valid_identifier(entry.target.target_id):
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_UNRESTORABLE,
            detail=(
                f"the recorded id {entry.target.target_id!r} does not have the shape of "
                f"a {action.title} id. Clamper will not build a call from it"
            ),
        )
    if not REGION.match(entry.target.region):
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_UNRESTORABLE,
            detail=f"the recorded region {entry.target.region!r} is not a region name",
        )

    handler = handler_for(action)

    # 3 and 4. Exists, and unchanged since the clamp.
    try:
        live = handler.live_state(clients, entry)
    except (ClientError, BotoCoreError) as exc:
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_ERROR,
            detail=f"could not read the resource's current state: {_describe(exc)}",
        )

    if live is None:
        # Closed as release-failed so it stops appearing as active, unless
        # this is a dry run, which writes nothing at all.
        closed = (
            entry
            if actuator.dry_run
            else ledger.mark_released(
                entry,
                _synthetic(
                    entry,
                    code="ResourceGone",
                    message="the resource no longer exists; nothing to restore",
                ),
            )
        )
        return ReleaseOutcome(
            entry=closed,
            outcome=OUTCOME_GONE,
            detail="the resource no longer exists, so there is nothing to restore",
        )

    conflict = dict(live) != dict(entry.applied_state)
    if conflict and not force:
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_CONFLICT,
            detail=(
                f"changed since the clamp: Clamper set {_state(entry.applied_state)}, it is "
                f"now {_state(live)}. Somebody changed it by hand, so it was left alone. "
                f"Re-run with --force to restore {_state(entry.previous_state)} anyway"
            ),
            live_state=live,
        )

    # 5. Sane recorded values.
    try:
        phase = handler.release_phase(entry)
        params = handler.release_params(entry)
    except ValueError as exc:
        return ReleaseOutcome(
            entry=entry, outcome=OUTCOME_UNRESTORABLE, detail=str(exc), live_state=live
        )

    result = actuator.invoke(
        Call(
            action_id=action.id,
            phase=phase,
            params=params,
            target_id=entry.target.target_id,
            region=entry.target.region,
        )
    )

    if result.dry_run:
        # Nothing is written. A dry-run release recorded as anything would
        # make the ledger claim something happened that did not.
        return ReleaseOutcome(
            entry=entry,
            outcome=OUTCOME_WOULD_RELEASE,
            detail=f"would restore {_state(entry.previous_state)}",
            forced=conflict,
            live_state=live,
        )

    marked = ledger.mark_released(entry, result)
    if result.performed:
        detail = f"restored {_state(entry.previous_state)}"
        if conflict:
            detail += f" (overrode a conflict: it was {_state(live)})"
        return ReleaseOutcome(
            entry=marked,
            outcome=OUTCOME_RELEASED,
            detail=detail,
            forced=conflict,
            live_state=live,
        )
    return ReleaseOutcome(
        entry=marked,
        outcome=OUTCOME_FAILED,
        detail=result.summary,
        forced=conflict,
        live_state=live,
    )


def _synthetic(entry: Entry, *, code: str, message: str) -> Result:
    """A failed `Result` for an outcome Clamper determined without a call.

    Used to close an entry whose resource is gone. There is deliberately no
    way to build a successful one: a synthetic success would be a release
    nobody performed.
    """
    return Result(
        call=Call(
            action_id=entry.target.action_id,
            phase="release",
            params={},
            target_id=entry.target.target_id,
            region=entry.target.region,
        ),
        ok=False,
        error_code=code,
        error_message=message,
    )


def _state(state: Mapping[str, Any]) -> str:
    if not state:
        return "(nothing recorded)"
    return ", ".join(
        f"{key}={'none' if value is None else value}" for key, value in sorted(state.items())
    )


def _describe(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        return f"{error.get('Code') or 'ClientError'} - {error.get('Message') or ''}".strip()
    return str(exc)
