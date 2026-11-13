"""`clamper.yml`: what may be stopped, and when.

This file decides what Clamper is allowed to stop, which makes it the most
consequential input in the tool. It is validated strictly for that reason:

**An unknown key is an error, not a warning.** Crosswind warns about these,
because a misspelled key there means a severity rule that did not apply and
a report that is slightly wrong. Here, `protect_tags:` instead of
`protect_tag:` means a resource somebody believed was protected being
stopped. The failure modes are not comparable, so the handling is not either.

Everything else follows from the same reasoning:

- There is **no key that names a boto3 method, an IAM action, or an ARN**.
  Resource types are referenced by action id and looked up in `actions.yml`;
  an unknown id is a load error. A config file cannot name something for
  Clamper to call.
- `mode` defaults to `dry-run`, and `clamper init` writes it explicitly with
  a comment. A default that is absent from the file is a default nobody
  reads.
- Widening the selection to untagged resources needs **two** keys, one of
  which is a sentence. See `include_untagged`.

This module is importable inside a Lambda: standard library and `yaml` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clamper.core.actions import Registry, load_registry

CONFIG_FILENAME = "clamper.yml"

#: Alternative name, because half the world writes `.yaml`.
CONFIG_FILENAME_ALT = "clamper.yaml"

SCHEMA_VERSION = 1

MODE_DRY_RUN = "dry-run"
MODE_ENFORCE = "enforce"
MODES = (MODE_DRY_RUN, MODE_ENFORCE)

DEFAULT_MANAGED_TAG = "clamper:managed"
DEFAULT_MANAGED_TAG_VALUE = "true"
DEFAULT_PROTECT_TAG = "clamper:protect"
DEFAULT_STACK_NAME = "clamper"

DEFAULT_WARN_PERCENT = 80.0
DEFAULT_CLAMP_PERCENT = 100.0

#: Above this, Clamper stops and notifies rather than acting. A plan far
#: bigger than an account's normal shape is more likely to be a bug or a
#: mis-applied tag than a real need. See docs/design.md.
DEFAULT_MAX_ACTIONS_PER_RUN = 50

#: Budgets refreshes up to three times a day, so six hours is already twice
#: as often as the data moves. docs/design.md has the AWS quote.
DEFAULT_RECONCILE_HOURS = 6

#: The exact sentence `include_untagged: true` requires alongside it.
#:
#: Deliberately a sentence rather than a boolean. The difference between
#: "stop the things I labelled" and "stop everything you can find" should
#: not be one character of YAML, and a reviewer should notice it in a diff.
UNTAGGED_ACKNOWLEDGEMENT = "I understand this can stop resources nobody tagged"

NOTIFICATION_ACTUAL = "actual"
NOTIFICATION_FORECASTED = "forecasted"
NOTIFICATION_TYPES = (NOTIFICATION_ACTUAL, NOTIFICATION_FORECASTED)

SCOPE_ACCOUNT = "account"
SCOPE_TAG = "tag"
SCOPE_KINDS = (SCOPE_ACCOUNT, SCOPE_TAG)

#: Limit ids end up in a budget name and a CloudFormation logical id, so they
#: are restricted to what both accept.
LIMIT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: A CloudFormation stack name, as AWS defines it.
STACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,118}$")

#: `us-east-1`, `eu-west-2`, `us-gov-west-1`, `ap-southeast-4`.
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

TOP_LEVEL_KEYS = frozenset(
    {"version", "mode", "stack_name", "regions", "limits", "targets", "safety", "notifications"}
)
LIMIT_KEYS = frozenset(
    {"id", "amount_usd", "scope", "warn_percent", "clamp_percent", "notification_type"}
)
SCOPE_KEYS = frozenset({"kind", "tag", "value"})
TARGET_KEYS = frozenset(
    {
        "managed_tag",
        "managed_tag_value",
        "protect_tag",
        "include_untagged",
        "untagged_acknowledgement",
        "types",
        "selectors",
    }
)
SELECTOR_KEYS = frozenset({"type", "ids"})
SAFETY_KEYS = frozenset({"max_actions_per_run", "reconcile_hours"})
NOTIFICATION_KEYS = frozenset({"email", "slack_webhook_parameter"})


class ConfigError(ValueError):
    """The config is not usable.

    Always raised with the key path that is wrong and what was expected.
    Nobody should have to read this module to work out which line to change.
    """


@dataclass(frozen=True)
class Scope:
    """What a limit measures: the whole account, or one cost allocation tag.

    A tag scope becomes a budget `CostFilters` entry of the form
    `user:<tag>$<value>`. The tag has to be **activated as a cost allocation
    tag** first, and activation is not retroactive — `clamper doctor` checks
    it, because a tag-scoped budget on an inactive tag reads as $0.00 forever
    and looks exactly like nothing being wrong.
    """

    kind: str = SCOPE_ACCOUNT
    tag: str | None = None
    value: str | None = None

    @property
    def cost_filter(self) -> dict[str, list[str]]:
        """The budget's `CostFilters`, or `{}` for a whole-account limit."""
        if self.kind == SCOPE_TAG and self.tag and self.value is not None:
            return {"TagKeyValue": [f"user:{self.tag}${self.value}"]}
        return {}

    @property
    def label(self) -> str:
        if self.kind == SCOPE_TAG:
            return f"tag {self.tag}={self.value}"
        return "whole account"


@dataclass(frozen=True)
class Limit:
    """One monthly USD limit, and the two thresholds on it.

    Thresholds are percentages rather than amounts because that is what a
    Budgets notification takes, so the number here and the number in the
    alert AWS sends are the same number.
    """

    id: str
    amount_usd: float
    scope: Scope = field(default_factory=Scope)
    warn_percent: float = DEFAULT_WARN_PERCENT
    clamp_percent: float = DEFAULT_CLAMP_PERCENT
    notification_type: str = NOTIFICATION_ACTUAL

    @property
    def warn_amount(self) -> float:
        return self.amount_usd * self.warn_percent / 100.0

    @property
    def clamp_amount(self) -> float:
        return self.amount_usd * self.clamp_percent / 100.0

    def budget_name(self, stack_name: str) -> str:
        """The budget AWS will hold for this limit.

        Derived rather than configurable: the enforcer has to find the budget
        by name from inside a Lambda that only knows the stack name, and a
        separately-settable name is one more thing that can disagree.
        """
        return f"{stack_name}-{self.id}"


@dataclass(frozen=True)
class Selector:
    """Explicit opt-in by resource id, as an alternative to the managed tag.

    Worth preferring in an account where `ec2:CreateTags` is widely held:
    a tag is opt-in that anyone who can tag can grant, and a selector is
    opt-in that only someone who can change this file can grant.
    """

    type: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class Targets:
    """Which resources are candidates at all.

    Nothing here can *cause* a clamp. It can only ever narrow the set, except
    for `include_untagged`, which is the one widening knob and is guarded by
    an acknowledgement sentence.
    """

    managed_tag: str = DEFAULT_MANAGED_TAG
    managed_tag_value: str = DEFAULT_MANAGED_TAG_VALUE
    protect_tag: str = DEFAULT_PROTECT_TAG
    include_untagged: bool = False
    types: tuple[str, ...] = ()
    selectors: tuple[Selector, ...] = ()

    def ids_for(self, action_id: str) -> frozenset[str]:
        """Explicitly selected resource ids for one action type."""
        chosen: set[str] = set()
        for selector in self.selectors:
            if selector.type == action_id:
                chosen.update(selector.ids)
        return frozenset(chosen)


@dataclass(frozen=True)
class Safety:
    """The limits on Clamper itself."""

    max_actions_per_run: int = DEFAULT_MAX_ACTIONS_PER_RUN
    reconcile_hours: int = DEFAULT_RECONCILE_HOURS


@dataclass(frozen=True)
class Notifications:
    """Where a clamp gets announced.

    `slack_webhook_parameter` is the **name** of an SSM `SecureString`
    parameter, never the URL. A Slack webhook URL is a credential — anyone
    holding it can post to that channel as you — and a CloudFormation
    template parameter is readable by anyone with
    `cloudformation:DescribeStacks`.
    """

    email: str | None = None
    slack_webhook_parameter: str | None = None


@dataclass(frozen=True)
class Config:
    """A validated `clamper.yml`.

    Defaults here are the defaults of the tool, so this dataclass doubles as
    the documentation for them.
    """

    mode: str = MODE_DRY_RUN
    stack_name: str = DEFAULT_STACK_NAME
    regions: tuple[str, ...] = ()
    limits: tuple[Limit, ...] = ()
    targets: Targets = field(default_factory=Targets)
    safety: Safety = field(default_factory=Safety)
    notifications: Notifications = field(default_factory=Notifications)
    #: Where this came from, for `doctor` and every report header.
    source: str | None = None

    @property
    def dry_run(self) -> bool:
        return self.mode != MODE_ENFORCE

    def limit(self, limit_id: str) -> Limit | None:
        for item in self.limits:
            if item.id == limit_id:
                return item
        return None


def find_config(start: Path) -> Path | None:
    """The nearest `clamper.yml` at or above `start`.

    Walks up so a monorepo can keep one file at the root. Stops at the
    filesystem root and never looks in `$HOME`: a config outside the
    repository is a surprise to anyone reading the repository, and this
    particular file decides what gets stopped.
    """
    current = start.resolve() if start.is_dir() else start.resolve().parent
    for directory in [current, *current.parents]:
        for name in (CONFIG_FILENAME, CONFIG_FILENAME_ALT):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load(path: Path, *, registry: Registry | None = None) -> Config:
    """Read and validate a config file.

    There is no "return the defaults if there is no file" path, unlike
    Crosswind's loader. A tool that can stop your infrastructure should not
    run on an implied configuration.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    return loads(text, source=str(path), registry=registry)


def loads(text: str, *, source: str | None = None, registry: Registry | None = None) -> Config:
    """Validate config from a string. The whole of validation lives here."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{_where(source)} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{_where(source)} is empty. `clamper init` writes a starting point.")
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(source)} should contain a mapping, not {type(raw).__name__}.")

    _reject_unknown(raw, TOP_LEVEL_KEYS, "", source)

    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"{_where(source)}: `version` must be {SCHEMA_VERSION}, got {version!r}. "
            f"A missing version is an error rather than an assumption: the schema "
            f"decides what can be stopped."
        )

    mode = str(raw.get("mode", MODE_DRY_RUN))
    if mode not in MODES:
        raise ConfigError(
            f"{_where(source)}: `mode` must be one of {', '.join(MODES)}, got {mode!r}."
        )

    stack_name = str(raw.get("stack_name", DEFAULT_STACK_NAME))
    if not STACK_NAME.match(stack_name):
        raise ConfigError(
            f"{_where(source)}: `stack_name` must start with a letter and contain only "
            f"letters, digits and hyphens, got {stack_name!r}."
        )

    regions = tuple(_as_str_list(raw.get("regions"), "regions", source))
    for region in regions:
        if not REGION.match(region):
            raise ConfigError(
                f"{_where(source)}: {region!r} is not an AWS region name. "
                f"Expected something like `us-east-1`."
            )

    registry = registry if registry is not None else load_registry()
    limits = _limits(raw.get("limits"), source)
    targets = _targets(raw.get("targets"), registry, source)
    safety = _safety(raw.get("safety"), source)
    notifications = _notifications(raw.get("notifications"), source)

    return Config(
        mode=mode,
        stack_name=stack_name,
        regions=regions,
        limits=limits,
        targets=targets,
        safety=safety,
        notifications=notifications,
        source=source,
    )


# --- sections -----------------------------------------------------------


def _limits(raw: Any, source: str | None) -> tuple[Limit, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"{_where(source)}: `limits` must be a non-empty list. "
            f"A Clamper with no limit has nothing to enforce."
        )

    limits: list[Limit] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"limits[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{_where(source)}: `{where}` must be a mapping.")
        _reject_unknown(item, LIMIT_KEYS, where, source)

        limit_id = str(item.get("id", "")).strip()
        if not LIMIT_ID.match(limit_id):
            raise ConfigError(
                f"{_where(source)}: `{where}.id` must be lowercase letters, digits and "
                f"hyphens, starting with a letter or digit, got {limit_id!r}. "
                f"It becomes part of a budget name."
            )
        if limit_id in seen:
            raise ConfigError(
                f"{_where(source)}: two limits share the id {limit_id!r}. "
                f"Ids name a budget, so they have to be unique."
            )
        seen.add(limit_id)

        amount = _number(item.get("amount_usd"), f"{where}.amount_usd", source)
        if amount <= 0:
            raise ConfigError(
                f"{_where(source)}: `{where}.amount_usd` must be greater than 0, got {amount}."
            )

        warn = _percent(
            item.get("warn_percent", DEFAULT_WARN_PERCENT), f"{where}.warn_percent", source
        )
        clamp = _percent(
            item.get("clamp_percent", DEFAULT_CLAMP_PERCENT), f"{where}.clamp_percent", source
        )
        if warn > clamp:
            raise ConfigError(
                f"{_where(source)}: `{where}.warn_percent` ({warn}) is above "
                f"`clamp_percent` ({clamp}). The warning has to come first or it is "
                f"not a warning."
            )

        notification_type = str(item.get("notification_type", NOTIFICATION_ACTUAL)).lower()
        if notification_type not in NOTIFICATION_TYPES:
            raise ConfigError(
                f"{_where(source)}: `{where}.notification_type` must be one of "
                f"{', '.join(NOTIFICATION_TYPES)}, got {notification_type!r}."
            )

        limits.append(
            Limit(
                id=limit_id,
                amount_usd=amount,
                scope=_scope(item.get("scope"), where, source),
                warn_percent=warn,
                clamp_percent=clamp,
                notification_type=notification_type,
            )
        )
    return tuple(limits)


def _scope(raw: Any, where: str, source: str | None) -> Scope:
    if raw is None:
        return Scope()
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(source)}: `{where}.scope` must be a mapping.")
    _reject_unknown(raw, SCOPE_KEYS, f"{where}.scope", source)

    kind = str(raw.get("kind", SCOPE_ACCOUNT)).lower()
    if kind not in SCOPE_KINDS:
        raise ConfigError(
            f"{_where(source)}: `{where}.scope.kind` must be one of "
            f"{', '.join(SCOPE_KINDS)}, got {kind!r}."
        )
    if kind == SCOPE_ACCOUNT:
        for key in ("tag", "value"):
            if key in raw:
                raise ConfigError(
                    f"{_where(source)}: `{where}.scope.{key}` has no meaning with "
                    f"`kind: account`. Remove it, or set `kind: tag`."
                )
        return Scope(kind=SCOPE_ACCOUNT)

    tag = str(raw.get("tag", "")).strip()
    if not tag:
        raise ConfigError(
            f"{_where(source)}: `{where}.scope.tag` is required with `kind: tag`. "
            f"It must be a cost allocation tag you have activated."
        )
    if "value" not in raw:
        raise ConfigError(
            f"{_where(source)}: `{where}.scope.value` is required with `kind: tag`. "
            f"A tag key with no value would scope the budget to every value of "
            f"that tag, which is almost never what someone means."
        )
    return Scope(kind=SCOPE_TAG, tag=tag, value=str(raw["value"]))


def _targets(raw: Any, registry: Registry, source: str | None) -> Targets:
    known = registry.ids()
    if raw is None:
        return Targets(types=tuple(known))
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(source)}: `targets` must be a mapping.")
    _reject_unknown(raw, TARGET_KEYS, "targets", source)

    managed_tag = _tag_key(
        raw.get("managed_tag", DEFAULT_MANAGED_TAG), "targets.managed_tag", source
    )
    protect_tag = _tag_key(
        raw.get("protect_tag", DEFAULT_PROTECT_TAG), "targets.protect_tag", source
    )
    if managed_tag == protect_tag:
        raise ConfigError(
            f"{_where(source)}: `managed_tag` and `protect_tag` are both "
            f"{managed_tag!r}. One enrols a resource and the other exempts it, so "
            f"they cannot be the same key."
        )

    managed_value = str(raw.get("managed_tag_value", DEFAULT_MANAGED_TAG_VALUE))

    include_untagged = bool(raw.get("include_untagged", False))
    acknowledgement = raw.get("untagged_acknowledgement")
    if include_untagged and acknowledgement != UNTAGGED_ACKNOWLEDGEMENT:
        raise ConfigError(
            f"{_where(source)}: `include_untagged: true` also needs\n"
            f'    untagged_acknowledgement: "{UNTAGGED_ACKNOWLEDGEMENT}"\n'
            f"spelled exactly that way. This widens Clamper from "
            f'"stop the things I labelled" to "stop everything you can find", '
            f"and that should not be one character of YAML."
        )
    if acknowledgement is not None and not include_untagged:
        raise ConfigError(
            f"{_where(source)}: `untagged_acknowledgement` is set but "
            f"`include_untagged` is not true. Remove one of them, so the file does "
            f"not read as though untagged resources are in scope when they are not."
        )

    if "types" in raw:
        types = tuple(_as_str_list(raw.get("types"), "targets.types", source))
        if not types:
            raise ConfigError(
                f"{_where(source)}: `targets.types` is empty, which disables every "
                f"action. Remove the key to enable all of them, or name the ones "
                f"you want."
            )
        unknown = [item for item in types if item not in known]
        if unknown:
            raise ConfigError(
                f"{_where(source)}: `targets.types` names {unknown!r}, which "
                f"{'is' if len(unknown) == 1 else 'are'} not a Clamper action. "
                f"Available: {', '.join(sorted(known))}."
            )
    else:
        types = tuple(known)

    selectors = _selectors(raw.get("selectors"), known, source)

    return Targets(
        managed_tag=managed_tag,
        managed_tag_value=managed_value,
        protect_tag=protect_tag,
        include_untagged=include_untagged,
        types=types,
        selectors=selectors,
    )


def _selectors(raw: Any, known: frozenset[str], source: str | None) -> tuple[Selector, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{_where(source)}: `targets.selectors` must be a list.")

    selectors: list[Selector] = []
    for index, item in enumerate(raw):
        where = f"targets.selectors[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{_where(source)}: `{where}` must be a mapping.")
        _reject_unknown(item, SELECTOR_KEYS, where, source)

        action_id = str(item.get("type", "")).strip()
        if action_id not in known:
            raise ConfigError(
                f"{_where(source)}: `{where}.type` is {action_id!r}, which is not a "
                f"Clamper action. Available: {', '.join(sorted(known))}."
            )
        ids = tuple(_as_str_list(item.get("ids"), f"{where}.ids", source))
        if not ids:
            raise ConfigError(
                f"{_where(source)}: `{where}.ids` must name at least one resource. "
                f"An empty selector selects nothing and reads as though it selects "
                f"everything."
            )
        selectors.append(Selector(type=action_id, ids=ids))
    return tuple(selectors)


def _safety(raw: Any, source: str | None) -> Safety:
    if raw is None:
        return Safety()
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(source)}: `safety` must be a mapping.")
    _reject_unknown(raw, SAFETY_KEYS, "safety", source)

    cap = _positive_int(
        raw.get("max_actions_per_run", DEFAULT_MAX_ACTIONS_PER_RUN),
        "safety.max_actions_per_run",
        source,
    )
    hours = _positive_int(
        raw.get("reconcile_hours", DEFAULT_RECONCILE_HOURS), "safety.reconcile_hours", source
    )
    if hours > 24:
        raise ConfigError(
            f"{_where(source)}: `safety.reconcile_hours` is {hours}. Budget data "
            f"refreshes up to three times a day, so anything above 24 means a "
            f"breach can sit unenforced for longer than a day."
        )
    return Safety(max_actions_per_run=cap, reconcile_hours=hours)


def _notifications(raw: Any, source: str | None) -> Notifications:
    if raw is None:
        return Notifications()
    if not isinstance(raw, dict):
        raise ConfigError(f"{_where(source)}: `notifications` must be a mapping.")
    _reject_unknown(raw, NOTIFICATION_KEYS, "notifications", source)

    email = raw.get("email")
    if email is not None:
        email = str(email).strip()
        # Not a full RFC check, which nothing short of sending a message is.
        # This catches the mistakes that produce a silently undeliverable
        # subscription: a name with no domain, a stray comma, whitespace.
        if not re.match(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$", email):
            raise ConfigError(
                f"{_where(source)}: `notifications.email` is {email!r}, which is not "
                f"a single email address. One address only; SNS takes more "
                f"subscribers after the stack exists."
            )

    parameter = raw.get("slack_webhook_parameter")
    if parameter is not None:
        parameter = str(parameter).strip()
        _ssm_parameter_name(parameter, "notifications.slack_webhook_parameter", source)

    return Notifications(email=email, slack_webhook_parameter=parameter)


# --- primitives ---------------------------------------------------------


def _where(source: str | None) -> str:
    return source or "clamper.yml"


def _reject_unknown(
    raw: dict[Any, Any], known: frozenset[str], prefix: str, source: str | None
) -> None:
    """An unknown key is an error.

    The whole reason this function exists rather than a warning: a
    misspelled key in this file is a setting somebody believes is in effect,
    and the setting they most often believe is in effect is the one that
    stops a resource being touched.
    """
    unknown = sorted(str(key) for key in raw if str(key) not in known)
    if not unknown:
        return
    at = f"`{prefix}`" if prefix else "the top level"
    suggestions = ", ".join(sorted(known))
    guesses = [
        f"{key} -> did you mean `{_closest(key, known)}`?"
        for key in unknown
        if _closest(key, known)
    ]
    detail = ("\n    " + "\n    ".join(guesses)) if guesses else ""
    raise ConfigError(
        f"{_where(source)}: unknown key{'s' if len(unknown) > 1 else ''} at {at}: "
        f"{', '.join(unknown)}.{detail}\n"
        f"    Known keys: {suggestions}.\n"
        f"    Unknown keys are errors rather than warnings here, because a "
        f"misspelled one is a setting you believe is in effect."
    )


def _closest(key: str, known: frozenset[str]) -> str | None:
    """A cheap "did you mean" for the common typo shapes.

    Plural/singular and a one-character slip cover nearly every real case;
    anything cleverer would need a dependency to guess worse.
    """
    for candidate in sorted(known):
        if key.rstrip("s") == candidate.rstrip("s") or key.replace("-", "_") == candidate:
            return candidate
    return None


def _as_str_list(value: Any, where: str, source: str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise ConfigError(
            f"{_where(source)}: `{where}` must be a list, not a single string. "
            f"Write `- {value}` on its own line."
        )
    if not isinstance(value, list):
        raise ConfigError(f"{_where(source)}: `{where}` must be a list.")
    items = [str(item).strip() for item in value]
    if any(not item for item in items):
        raise ConfigError(f"{_where(source)}: `{where}` contains an empty entry.")
    return items


def _number(value: Any, where: str, source: str | None) -> float:
    if isinstance(value, bool) or value is None:
        raise ConfigError(f"{_where(source)}: `{where}` must be a number.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{_where(source)}: `{where}` must be a number, got {value!r}.") from exc


def _percent(value: Any, where: str, source: str | None) -> float:
    number = _number(value, where, source)
    # Above 100 is legitimate — "clamp at 120% of budget" is a real policy —
    # but a four-figure percentage is a units mistake, usually an amount
    # typed into a percentage field.
    if not 0 < number <= 1000:
        raise ConfigError(
            f"{_where(source)}: `{where}` must be a percentage above 0 and at most "
            f"1000, got {number}. It is a percentage of the limit, not an amount."
        )
    return number


def _positive_int(value: Any, where: str, source: str | None) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{_where(source)}: `{where}` must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{_where(source)}: `{where}` must be a whole number, got {value!r}."
        ) from exc
    if number < 1:
        raise ConfigError(f"{_where(source)}: `{where}` must be at least 1, got {number}.")
    return number


def _tag_key(value: Any, where: str, source: str | None) -> str:
    key = str(value).strip()
    if not key:
        raise ConfigError(
            f"{_where(source)}: `{where}` cannot be empty. An empty tag key would "
            f"match nothing, and for `protect_tag` that means nothing is protected."
        )
    # AWS's own tag key rules, minus the length ceiling nobody reaches.
    if not re.match(r"^[\w\s+\-=.:/@]{1,128}$", key, re.UNICODE):
        raise ConfigError(
            f"{_where(source)}: `{where}` is {key!r}, which is not a valid AWS tag "
            f"key. Letters, digits and `+ - = . : / @ _` only."
        )
    return key


def _ssm_parameter_name(value: str, where: str, source: str | None) -> str:
    """An SSM parameter name, checked for traversal.

    The enforcer reads this parameter with its own role, so a name that
    escapes its prefix is a way to make Clamper read a parameter it was not
    meant to. The role is scoped to a path as well; this is the first layer.
    """
    # The URL check comes first. Pasting the webhook here is the mistake
    # somebody will actually make, and "must start with /" is a useless thing
    # to tell them when the real answer is "that is a credential, put it in
    # Parameter Store".
    if "://" in value or "hooks.slack.com" in value:
        raise ConfigError(
            f"{_where(source)}: `{where}` looks like a webhook URL. Put the URL in "
            f"SSM Parameter Store as a SecureString and name the parameter here:\n"
            f"    aws ssm put-parameter --name /clamper/slack-webhook "
            f"--type SecureString --value <url>\n"
            f"then set `{where}: /clamper/slack-webhook`. A webhook URL is a "
            f"credential: anyone holding it can post to that channel as you."
        )
    if not value.startswith("/"):
        raise ConfigError(
            f"{_where(source)}: `{where}` must be an SSM parameter name starting "
            f"with `/`, got {value!r}. This is the parameter's **name**, never the "
            f"webhook URL itself."
        )
    if ".." in value or "//" in value:
        raise ConfigError(
            f"{_where(source)}: `{where}` is {value!r}, which contains a path "
            f"traversal. Parameter names must be literal."
        )
    if not re.match(r"^(/[A-Za-z0-9_.-]+)+$", value):
        raise ConfigError(
            f"{_where(source)}: `{where}` is {value!r}, which is not a valid SSM parameter name."
        )
    return value
