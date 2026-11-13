"""The action registry: what Clamper is allowed to do, loaded from data.

[`data/actions.yml`](../data/actions.yml) declares every write Clamper can
make. This module loads it, validates it hard, and is the only place that
decides what a valid write is. Three other things are derived from what it
returns rather than written separately:

- `core/actuator.py` takes the boto3 method name from here, by action id. A
  caller never names a method, so a caller cannot name a bad one.
- `core/policy.py` generates the enforcer's IAM policy from these rows.
- `clamper.yml`'s `targets.types` is validated against `ids()`.

The load-time refusals are the point of the module. An action whose boto3
method or IAM action starts with a destructive verb raises
`DestructiveAction`, which happens at import time for anyone who touches the
registry — so a bad row makes the package fail to load rather than shipping
quietly. There is exactly one named exception and it is a literal string with
its reason written beside it in the YAML.

Importable inside a Lambda: standard library and `yaml` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

DATA_PACKAGE = "clamper.data"
DATA_FILE = "actions.yml"

SCHEMA_VERSION = 1

#: A boto3 method name: `stop_instances`, `update_auto_scaling_group`.
METHOD = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: An IAM action: `ec2:StopInstances`.
IAM_ACTION = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9]+$")

#: An action id: used in config, in the ledger, and in a report.
ACTION_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

PHASE_CLAMP = "clamp"
PHASE_RELEASE = "release"
PHASE_RELEASE_ABSENT = "release_absent"
PHASES = (PHASE_CLAMP, PHASE_RELEASE, PHASE_RELEASE_ABSENT)

ACTION_KEYS = frozenset(
    {
        "id",
        "title",
        "client",
        "identifier",
        "identifier_pattern",
        "arn_pattern",
        "tag_condition",
        "reversible",
        "restart_after_days",
        "discover",
        "records",
        "clamp",
        "release",
        "release_absent",
        "notes",
    }
)
OPERATION_KEYS = frozenset({"method", "iam", "params"})

#: Read-only verb prefixes. Every `discover` action has to start with one, so
#: a write cannot be smuggled in as a discovery permission — which would be a
#: write Clamper could make that the actuator never sees.
READ_PREFIXES = ("Describe", "Get", "List")


class RegistryError(ValueError):
    """`actions.yml` is malformed. Loud, with the row that is wrong."""


class DestructiveAction(RegistryError):
    """An action declares a call that could lose data.

    Its own class because this is the refusal that matters: v1 is reversible
    actions only, and that is enforced here rather than reviewed.
    """


class UnknownAction(KeyError):
    """An action id nobody declared.

    Raised by the actuator when asked for an id that is not in the registry,
    which is how a ledger entry or a config naming a type that no longer
    exists surfaces as an error rather than as a silent no-op.
    """


@dataclass(frozen=True)
class Operation:
    """One AWS call: the method, the permission, and the parameters.

    `params` is a closed list. The actuator rejects a call carrying a key
    that is not in it, because `Force=True` arriving as an unchecked
    passthrough is the shape of this whole class of bug.
    """

    phase: str
    method: str
    iam: str
    params: tuple[str, ...]


@dataclass(frozen=True)
class Action:
    """One declared, reversible thing Clamper can do to a resource type."""

    id: str
    title: str
    client: str
    identifier: str
    identifier_pattern: str
    arn_pattern: str
    tag_condition: str | None
    discover: tuple[str, ...]
    records: tuple[str, ...]
    clamp: Operation
    release: Operation
    release_absent: Operation | None
    restart_after_days: int | None
    notes: tuple[str, ...]

    def operation(self, phase: str) -> Operation:
        """The operation for a phase, or `UnknownAction` if there is none."""
        if phase == PHASE_CLAMP:
            return self.clamp
        if phase == PHASE_RELEASE:
            return self.release
        if phase == PHASE_RELEASE_ABSENT:
            if self.release_absent is None:
                raise UnknownAction(
                    f"{self.id} has no `release_absent` operation. Only an action "
                    f"whose previous state can be genuinely absent needs one."
                )
            return self.release_absent
        raise UnknownAction(f"{phase!r} is not a phase. One of: {', '.join(PHASES)}.")

    @property
    def operations(self) -> tuple[Operation, ...]:
        if self.release_absent is None:
            return (self.clamp, self.release)
        return (self.clamp, self.release, self.release_absent)

    @property
    def restarts_itself(self) -> bool:
        """AWS undoes this clamp on its own after `restart_after_days`."""
        return self.restart_after_days is not None

    def valid_identifier(self, value: str) -> bool:
        """Whether `value` has the shape this action's resource ids have.

        Checked on every ledger entry before a release call is built from
        it, and on every candidate before it can be clamped. The second check
        is the one that matters most: a resource Clamper could stop but not
        then release would be a clamp with no undo.
        """
        return re.fullmatch(self.identifier_pattern, value) is not None

    def arn(self, account: str, region: str = "*") -> str:
        return self.arn_pattern.format(account=account, region=region)


@dataclass(frozen=True)
class Registry:
    """Every declared action, and the allowlists derived from them.

    The allowlists are properties rather than stored fields so that there is
    no way for them to drift from `actions`: they are recomputed from the
    rows every time, and the rows are frozen.
    """

    actions: tuple[Action, ...]
    destructive_verbs: tuple[str, ...]
    destructive_verb_exceptions: frozenset[str]

    def ids(self) -> frozenset[str]:
        return frozenset(action.id for action in self.actions)

    def get(self, action_id: str) -> Action:
        for action in self.actions:
            if action.id == action_id:
                return action
        available = ", ".join(sorted(self.ids()))
        raise UnknownAction(f"{action_id!r} is not a Clamper action. Available: {available}.")

    def enabled(self, types: tuple[str, ...]) -> tuple[Action, ...]:
        """The actions a config turned on, in declaration order.

        Declaration order rather than config order, so two configs listing
        the same types produce the same plan and the same report.
        """
        chosen = set(types)
        return tuple(action for action in self.actions if action.id in chosen)

    @property
    def methods(self) -> frozenset[str]:
        """Every boto3 method the actuator may invoke.

        `tests/test_safety.py` asserts the actuator's own allowlist equals
        this, and pins the set, so widening it has to be a deliberate edit.
        """
        return frozenset(
            operation.method for action in self.actions for operation in action.operations
        )

    @property
    def write_iam_actions(self) -> frozenset[str]:
        return frozenset(
            operation.iam for action in self.actions for operation in action.operations
        )

    @property
    def discover_iam_actions(self) -> frozenset[str]:
        return frozenset(item for action in self.actions for item in action.discover)

    def method_for(self, action_id: str, phase: str) -> str:
        """The one lookup the actuator uses. A caller never passes a method."""
        return self.get(action_id).operation(phase).method


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """Load and validate `actions.yml`.

    Cached, because it is read on every code path and the file cannot change
    under a running process. `load_registry.cache_clear()` exists for the
    tests that load a deliberately broken document.
    """
    text = resources.files(DATA_PACKAGE).joinpath(DATA_FILE).read_text(encoding="utf-8")
    return loads(text)


def loads(text: str) -> Registry:
    """Validate a registry document. All of validation is here."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"actions.yml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError("actions.yml should contain a mapping.")

    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise RegistryError(f"actions.yml `version` must be {SCHEMA_VERSION}, got {version!r}.")

    verbs = tuple(str(item).lower() for item in raw.get("destructive_verbs") or ())
    if not verbs:
        raise RegistryError(
            "actions.yml declares no `destructive_verbs`. An empty list would make "
            "every refusal below a no-op, which is worse than having no check at "
            "all because it would look like there was one."
        )
    exceptions = frozenset(str(item) for item in raw.get("destructive_verb_exceptions") or ())
    for item in exceptions:
        if not IAM_ACTION.match(item):
            raise RegistryError(
                f"actions.yml `destructive_verb_exceptions` contains {item!r}. Each "
                f"entry must be a literal IAM action such as "
                f"`lambda:DeleteFunctionConcurrency`. A pattern here would be a hole."
            )

    rows = raw.get("actions")
    if not isinstance(rows, list) or not rows:
        raise RegistryError("actions.yml `actions` must be a non-empty list.")

    actions: list[Action] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        action = _action(row, index, verbs, exceptions)
        if action.id in seen:
            raise RegistryError(
                f"actions.yml declares {action.id!r} twice. Ids appear in the ledger, "
                f"so they have to be unique and stable."
            )
        seen.add(action.id)
        actions.append(action)

    return Registry(
        actions=tuple(actions),
        destructive_verbs=verbs,
        destructive_verb_exceptions=exceptions,
    )


def _action(raw: Any, index: int, verbs: tuple[str, ...], exceptions: frozenset[str]) -> Action:
    where = f"actions[{index}]"
    if not isinstance(raw, dict):
        raise RegistryError(f"actions.yml `{where}` must be a mapping.")

    unknown = sorted(str(key) for key in raw if str(key) not in ACTION_KEYS)
    if unknown:
        raise RegistryError(
            f"actions.yml `{where}` has unknown key(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(ACTION_KEYS))}."
        )

    action_id = str(raw.get("id", "")).strip()
    if not ACTION_ID.match(action_id):
        raise RegistryError(
            f"actions.yml `{where}.id` is {action_id!r}; expected lowercase letters, "
            f"digits and hyphens."
        )
    where = f"actions[{index}] ({action_id})"

    # Checked before anything else is parsed, so a row that claims to be
    # irreversible fails on that rather than on some later detail.
    if raw.get("reversible") is not True:
        raise DestructiveAction(
            f"actions.yml `{where}` does not declare `reversible: true`. Clamper v1 "
            f"performs reversible actions only. An action that loses data or cannot "
            f"be undone does not belong in this file — see docs/actions.md for what "
            f"was left out of v1 and why."
        )

    title = str(raw.get("title", "")).strip()
    if not title:
        raise RegistryError(f"actions.yml `{where}.title` is required; it names the resource.")

    client = str(raw.get("client", "")).strip()
    if not re.match(r"^[a-z][a-z0-9-]{1,31}$", client):
        raise RegistryError(f"actions.yml `{where}.client` is {client!r}; expected a boto3 client.")

    identifier = str(raw.get("identifier", "")).strip()
    if not identifier:
        raise RegistryError(
            f"actions.yml `{where}.identifier` is required. It names the field a "
            f"ledger entry is validated against before a release is built."
        )

    identifier_pattern = str(raw.get("identifier_pattern", "")).strip()
    if not (identifier_pattern.startswith("^") and identifier_pattern.endswith("$")):
        raise RegistryError(
            f"actions.yml `{where}.identifier_pattern` must be an anchored regular "
            f"expression (`^...$`). An unanchored one accepts any string that merely "
            f"contains a valid id, which is not a check."
        )
    try:
        re.compile(identifier_pattern)
    except re.error as exc:
        raise RegistryError(
            f"actions.yml `{where}.identifier_pattern` does not compile: {exc}"
        ) from exc

    arn_pattern = str(raw.get("arn_pattern", "")).strip()
    if not arn_pattern.startswith("arn:"):
        raise RegistryError(
            f"actions.yml `{where}.arn_pattern` must be an ARN pattern; the generated "
            f"IAM policy scopes the write actions to it."
        )
    if "{account}" not in arn_pattern:
        raise RegistryError(
            f"actions.yml `{where}.arn_pattern` has no `{{account}}` placeholder. A "
            f"write statement that is not scoped to the account is one the policy "
            f"generator will refuse anyway."
        )

    tag_condition_raw = raw.get("tag_condition")
    tag_condition = None if tag_condition_raw is None else str(tag_condition_raw)
    if tag_condition is not None and not tag_condition.endswith("/"):
        raise RegistryError(
            f"actions.yml `{where}.tag_condition` is {tag_condition!r}. It is a "
            f"condition key *prefix* and the tag key is appended to it, so it has to "
            f"end with `/` — for example `aws:ResourceTag/`."
        )

    discover = tuple(str(item) for item in raw.get("discover") or ())
    if not discover:
        raise RegistryError(
            f"actions.yml `{where}.discover` is empty. An action with no way to find "
            f"candidates can never clamp anything, which is a bug that would present "
            f"as 'nothing matched'."
        )
    for item in discover:
        if not IAM_ACTION.match(item):
            raise RegistryError(f"actions.yml `{where}.discover` has a bad action: {item!r}.")
        verb = item.partition(":")[2]
        if not verb.startswith(READ_PREFIXES):
            raise DestructiveAction(
                f"actions.yml `{where}.discover` contains {item!r}, which is not a "
                f"Describe/Get/List action. Discovery permissions are granted "
                f"unscoped, so a write among them would be a write Clamper can make "
                f"that the actuator never sees."
            )

    records = tuple(str(item) for item in raw.get("records") or ())
    if not records:
        raise RegistryError(
            f"actions.yml `{where}.records` is empty. With nothing recorded there is "
            f"nothing for `clamper release` to restore, which makes the action "
            f"irreversible in practice however reversible the API is."
        )

    clamp = _operation(raw.get("clamp"), PHASE_CLAMP, where, verbs, exceptions)
    release = _operation(raw.get("release"), PHASE_RELEASE, where, verbs, exceptions)
    release_absent = (
        _operation(raw.get("release_absent"), PHASE_RELEASE_ABSENT, where, verbs, exceptions)
        if raw.get("release_absent") is not None
        else None
    )

    # No check that clamp and release differ. They legitimately do not for
    # ECS and Auto Scaling, where the inverse is the same call carrying the
    # recorded value instead of zero. An earlier version asserted they
    # differed and rejected both of those rows.

    restart_after_days = raw.get("restart_after_days")
    if restart_after_days is not None:
        restart_after_days = int(restart_after_days)
        if restart_after_days < 1:
            raise RegistryError(f"actions.yml `{where}.restart_after_days` must be at least 1.")

    notes = tuple(" ".join(str(item).split()) for item in raw.get("notes") or ())

    return Action(
        id=action_id,
        title=title,
        client=client,
        identifier=identifier,
        identifier_pattern=identifier_pattern,
        arn_pattern=arn_pattern,
        tag_condition=tag_condition,
        discover=discover,
        records=records,
        clamp=clamp,
        release=release,
        release_absent=release_absent,
        restart_after_days=restart_after_days,
        notes=notes,
    )


def _operation(
    raw: Any, phase: str, where: str, verbs: tuple[str, ...], exceptions: frozenset[str]
) -> Operation:
    at = f"{where}.{phase}"
    if not isinstance(raw, dict):
        raise RegistryError(f"actions.yml `{at}` must be a mapping with method/iam/params.")
    unknown = sorted(str(key) for key in raw if str(key) not in OPERATION_KEYS)
    if unknown:
        raise RegistryError(f"actions.yml `{at}` has unknown key(s): {', '.join(unknown)}.")

    method = str(raw.get("method", "")).strip()
    if not METHOD.match(method):
        raise RegistryError(
            f"actions.yml `{at}.method` is {method!r}; expected a snake_case boto3 method name."
        )

    iam = str(raw.get("iam", "")).strip()
    if not IAM_ACTION.match(iam):
        raise RegistryError(f"actions.yml `{at}.iam` is {iam!r}; expected `service:ActionName`.")

    _refuse_destructive(method, iam, at, verbs, exceptions)

    params = tuple(str(item) for item in raw.get("params") or ())
    if not params:
        raise RegistryError(
            f"actions.yml `{at}.params` is empty. The actuator checks a call's keys "
            f"against this list, and an empty list would reject every call."
        )
    for item in params:
        if not re.match(r"^[A-Za-z][A-Za-z0-9]{0,63}$", item):
            raise RegistryError(f"actions.yml `{at}.params` has a bad name: {item!r}.")

    return Operation(phase=phase, method=method, iam=iam, params=params)


def _refuse_destructive(
    method: str, iam: str, at: str, verbs: tuple[str, ...], exceptions: frozenset[str]
) -> None:
    """Refuse a row whose call could lose data.

    Both spellings are checked — the boto3 method and the IAM action —
    because they are different strings and either one being wrong is enough
    to matter. `stop_db_instance` / `rds:StopDBInstance` agree; a row where
    they do not is itself suspicious.

    The exception list is matched on the **IAM action only**, exactly, and the
    boto3 method is then allowed to match it. That asymmetry is deliberate:
    the IAM action is the thing a reader of the policy sees, so it is the
    thing the exception has to be written in terms of.
    """
    if iam in exceptions:
        return

    iam_verb = iam.partition(":")[2]
    for verb in verbs:
        if method.startswith(f"{verb}_") or iam_verb.lower().startswith(verb):
            raise DestructiveAction(
                f"actions.yml `{at}` declares {iam} ({method}), whose verb "
                f"{verb!r} is on the destructive list. Clamper v1 performs "
                f"reversible actions only, and the registry refuses to load this "
                f"row rather than leaving it to review.\n"
                f"If this really is reversible and really is needed, add the exact "
                f"IAM action to `destructive_verb_exceptions` in actions.yml with "
                f"the reason written beside it. There is one such exception today "
                f"and it should stay rare."
            )
