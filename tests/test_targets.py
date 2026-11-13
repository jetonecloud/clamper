"""Target selection: the seven steps, self-exclusion, and the cap.

Pure functions over candidates, so no AWS at all. The candidates are built
through the real handlers' constructors, which enforce the same invariants
discovery does.
"""

from __future__ import annotations

from typing import Any

import pytest
from roundtrip import handler

from clamper.core.config import Config, Limit, Scope, loads
from clamper.core.cost import Price
from clamper.core.names import StackNames
from clamper.core.resources import Candidate
from clamper.core.resources.base import STACK_TAG
from clamper.core.targets import (
    CHECK_NAME,
    CHECK_STACK_RESOURCES,
    CHECK_STACK_TAG,
    DROP_OUT_OF_SCOPE,
    DROP_PROTECTED,
    DROP_SELF,
    DROP_TYPE_DISABLED,
    DROP_UNCLAMPABLE,
    ENV_STACK_NAME,
    ENV_STACK_RESOURCES,
    SelfIdentity,
    select,
)

MANAGED = {"clamper:managed": "true"}
IDENTITY = SelfIdentity(stack_name="clamper")


def config(extra: str = "") -> Config:
    return loads("version: 1\nlimits:\n  - id: whole-account\n    amount_usd: 500\n" + extra)


def ec2(
    instance_id: str = "i-0123456789abcdef0",
    *,
    tags: dict[str, str] | None = None,
    hourly: float | None = None,
) -> Candidate:
    return handler("ec2-instance").candidate(
        target_id=instance_id,
        region="us-east-1",
        description="m5.large, running",
        tags=MANAGED if tags is None else tags,
        state={"state": "running"},
        price=Price(hourly, "test") if hourly is not None else None,
    )


def fn(name: str, *, tags: dict[str, str] | None = None) -> Candidate:
    return handler("lambda-function").candidate(
        target_id=name,
        region="us-east-1",
        description="python3.12",
        tags=MANAGED if tags is None else tags,
        state={"reserved_concurrent_executions": None},
    )


def run(candidates: list[Candidate], cfg: Config | None = None, **kwargs: Any) -> Any:
    chosen = cfg or config()
    return select(candidates, chosen, chosen.limits, kwargs.pop("identity", IDENTITY), **kwargs)


# --- 3. selected --------------------------------------------------------


def test_a_managed_resource_is_a_target() -> None:
    assert [item.target_id for item in run([ec2()]).targets] == ["i-0123456789abcdef0"]


def test_an_untagged_resource_is_counted_not_listed() -> None:
    selection = run([ec2(tags={})])
    assert selection.targets == ()
    assert selection.dropped == ()
    assert selection.not_selected == 1


def test_the_managed_tag_value_has_to_match() -> None:
    assert run([ec2(tags={"clamper:managed": "false"})]).targets == ()


def test_a_selector_opts_in_by_id() -> None:
    cfg = config(
        "targets:\n  selectors:\n    - type: ec2-instance\n      ids: [i-0123456789abcdef0]\n"
    )
    assert len(run([ec2(tags={})], cfg).targets) == 1


def test_a_selector_for_another_type_does_not_match() -> None:
    cfg = config(
        "targets:\n  selectors:\n    - type: rds-instance\n      ids: [i-0123456789abcdef0]\n"
    )
    assert run([ec2(tags={})], cfg).targets == ()


def test_include_untagged_widens_with_its_acknowledgement() -> None:
    cfg = config(
        "targets:\n  include_untagged: true\n"
        '  untagged_acknowledgement: "I understand this can stop resources nobody tagged"\n'
    )
    assert len(run([ec2(tags={})], cfg).targets) == 1


# --- 1. scope -----------------------------------------------------------


def test_a_tag_scoped_limit_only_covers_its_tag() -> None:
    cfg = config()
    scoped = Limit(id="team", amount_usd=10, scope=Scope(kind="tag", tag="team", value="ml"))
    inside = ec2("i-0000000000000000a", tags={**MANAGED, "team": "ml"})
    outside = ec2("i-0000000000000000b", tags={**MANAGED, "team": "web"})

    selection = select([inside, outside], cfg, [scoped], IDENTITY)

    assert [item.target_id for item in selection.targets] == ["i-0000000000000000a"]
    assert [(item.candidate.target_id, item.category) for item in selection.dropped] == [
        ("i-0000000000000000b", DROP_OUT_OF_SCOPE)
    ]


# --- 2. enabled types ---------------------------------------------------


def test_a_disabled_type_is_dropped_with_the_reason() -> None:
    cfg = config("targets:\n  types: [rds-instance]\n")
    (dropped,) = run([ec2()], cfg).dropped
    assert dropped.category == DROP_TYPE_DISABLED


# --- 4. protect ---------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "false", "", "no"])
def test_the_protect_tag_wins_with_any_value(value: str) -> None:
    """Any value, including "false". A tag you can set in the console and be
    certain about is worth more than the flexibility of a value check."""
    (dropped,) = run([ec2(tags={**MANAGED, "clamper:protect": value})]).dropped
    assert dropped.category == DROP_PROTECTED


def test_protect_beats_a_selector_and_include_untagged() -> None:
    cfg = config(
        "targets:\n  include_untagged: true\n"
        '  untagged_acknowledgement: "I understand this can stop resources nobody tagged"\n'
        "  selectors:\n    - type: ec2-instance\n      ids: [i-0123456789abcdef0]\n"
    )
    selection = run([ec2(tags={"clamper:protect": "true"})], cfg)
    assert selection.targets == ()
    assert selection.dropped[0].category == DROP_PROTECTED


def test_every_protected_resource_is_listed_opted_in_or_not() -> None:
    """So a resource that silently stopped being protected is visible in the
    next plan."""
    selection = run([ec2(tags={"clamper:protect": "true"})])
    assert len(selection.protected) == 1


def test_a_custom_protect_tag_is_honoured() -> None:
    cfg = config("targets:\n  protect_tag: do-not-stop\n")
    assert run([ec2(tags={**MANAGED, "do-not-stop": "1"})], cfg).targets == ()
    # And the default key then means nothing.
    assert len(run([ec2(tags={**MANAGED, "clamper:protect": "1"})], cfg).targets) == 1


# --- 5. self-exclusion --------------------------------------------------

ENFORCER = StackNames("clamper").function


def enforcer(tags: dict[str, str] | None = None) -> Candidate:
    return fn(ENFORCER, tags={**MANAGED, **(tags or {})})


@pytest.mark.parametrize(
    ("check", "candidate", "identity"),
    [
        (
            CHECK_STACK_TAG,
            fn("renamed-fn", tags={**MANAGED, STACK_TAG: "clamper"}),
            SelfIdentity("clamper"),
        ),
        (
            CHECK_STACK_RESOURCES,
            fn("renamed-fn"),
            SelfIdentity(
                "clamper",
                resource_ids=frozenset(
                    {"arn:aws:lambda:us-east-1:123456789012:function:renamed-fn"}
                ),
            ),
        ),
        (CHECK_NAME, enforcer(), SelfIdentity("clamper")),
    ],
)
def test_each_self_check_excludes_on_its_own(
    check: str, candidate: Candidate, identity: SelfIdentity
) -> None:
    """All three have to fail for the bad outcome. Each is tested with the
    other two switched off, so none of them is load-bearing by accident."""
    alone = SelfIdentity(identity.stack_name, identity.resource_ids, checks=(check,))
    (dropped,) = run([candidate], identity=alone).dropped
    assert dropped.category == DROP_SELF
    others = tuple(
        item for item in (CHECK_STACK_TAG, CHECK_STACK_RESOURCES, CHECK_NAME) if item != check
    )
    without = SelfIdentity(identity.stack_name, identity.resource_ids, checks=others)
    assert len(run([candidate], identity=without).targets) == 1


def test_the_enforcer_is_excluded_even_with_include_untagged() -> None:
    cfg = config(
        "targets:\n  include_untagged: true\n"
        '  untagged_acknowledgement: "I understand this can stop resources nobody tagged"\n'
    )
    assert run([fn(ENFORCER, tags={})], cfg).targets == ()


def test_self_exclusion_happens_before_the_cap() -> None:
    """A run over the cap cannot have excluded the enforcer "later"."""
    cfg = config("safety:\n  max_actions_per_run: 1\n")
    selection = run([enforcer(), ec2()], cfg)
    assert selection.over_cap is False
    assert [item.target_id for item in selection.targets] == ["i-0123456789abcdef0"]


def test_the_identity_is_read_from_the_stack_environment() -> None:
    identity = SelfIdentity.from_env(
        {ENV_STACK_NAME: "clamper", ENV_STACK_RESOURCES: "clamper-ledger, arn:aws:sns:x:y:z"}
    )
    assert identity.stack_name == "clamper"
    assert identity.resource_ids == {"clamper-ledger", "arn:aws:sns:x:y:z"}


def test_the_enforcer_refuses_to_guess_its_own_stack() -> None:
    with pytest.raises(RuntimeError, match="does not guess"):
        SelfIdentity.from_env({})


# --- 6. clampable now ---------------------------------------------------


def test_an_unclampable_candidate_is_dropped_with_its_own_reason() -> None:
    stopped = handler("ec2-instance").unclampable(
        target_id="i-0123456789abcdef0",
        region="us-east-1",
        description="m5.large, stopped",
        tags=MANAGED,
        reason="already stopped",
    )
    (dropped,) = run([stopped]).dropped
    assert dropped.category == DROP_UNCLAMPABLE
    assert dropped.reason == "already stopped"


def test_an_id_that_could_not_be_released_is_never_clamped() -> None:
    (dropped,) = run([ec2("not-an-instance-id")]).dropped
    assert dropped.category == DROP_UNCLAMPABLE
    assert "could not be undone" in dropped.reason


# --- 7. the cap ---------------------------------------------------------


def test_over_the_cap_nothing_is_actionable() -> None:
    """Not the first fifty. Truncating would act on whichever ones the API
    returned first and leave the account half-stopped by accident."""
    cfg = config("safety:\n  max_actions_per_run: 2\n")
    selection = run([ec2(f"i-000000000000000{n}a") for n in range(3)], cfg)
    assert selection.over_cap is True
    assert len(selection.targets) == 3
    assert selection.actionable == ()


def test_at_the_cap_everything_is_actionable() -> None:
    cfg = config("safety:\n  max_actions_per_run: 3\n")
    selection = run([ec2(f"i-000000000000000{n}a") for n in range(3)], cfg)
    assert selection.over_cap is False
    assert len(selection.actionable) == 3


def test_targets_are_ordered_most_expensive_first() -> None:
    selection = run(
        [
            ec2("i-0000000000000000a", hourly=0.1),
            ec2("i-0000000000000000b"),
            ec2("i-0000000000000000c", hourly=32.77),
        ]
    )
    assert [item.target_id for item in selection.targets] == [
        "i-0000000000000000c",
        "i-0000000000000000a",
        "i-0000000000000000b",
    ]
