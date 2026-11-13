"""The generated IAM policies: what they grant, and what they refuse to.

Preflight's rule is "no write verb in the policy". Clamper has to write, so
the rule here is the inversion: no destructive verb, no wildcard action, and
no unscoped write — checked on every generated policy and on the committed
examples in iam/, which must equal what the generator emits today.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import ACCOUNT

from clamper.core.actions import load_registry
from clamper.core.config import Config, loads
from clamper.core.policy import (
    PolicyError,
    enforcer_policy,
    operator_policy,
    render,
    validate,
)

ROOT = Path(__file__).resolve().parent.parent
IAM = ROOT / "iam"

BASE = "version: 1\nlimits:\n  - id: whole-account\n    amount_usd: 500\n"


def config(extra: str = "") -> Config:
    return loads(BASE + extra)


def statements(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["Sid"]: item for item in policy["Statement"]}


def every_action(policy: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for item in policy["Statement"]:
        actions = item["Action"]
        found.update([actions] if isinstance(actions, str) else actions)
    return found


# --- what is granted ----------------------------------------------------


def test_the_writes_are_exactly_the_registrys() -> None:
    """The permission Clamper asks for and the thing Clamper can do are the
    same list by construction."""
    policy = enforcer_policy(config(), ACCOUNT)
    granted = {
        action
        for sid, item in statements(policy).items()
        if sid.endswith("Tagged")
        for action in item["Action"]
    }
    assert granted == load_registry().write_iam_actions


def test_each_write_carries_its_own_condition_key() -> None:
    """autoscaling:ResourceTag, not aws:ResourceTag. The general key on
    UpdateAutoScalingGroup is not an error — it just never matches."""
    found = statements(enforcer_policy(config(), ACCOUNT))
    asg = found["AutoscalingGroupTagged"]["Condition"]["StringEquals"]
    assert asg == {"autoscaling:ResourceTag/clamper:managed": "true"}
    ec2 = found["Ec2InstanceTagged"]["Condition"]["StringEquals"]
    assert ec2 == {"aws:ResourceTag/clamper:managed": "true"}


def test_writes_are_scoped_to_the_account() -> None:
    for sid, item in statements(enforcer_policy(config(), ACCOUNT)).items():
        if sid.endswith("Tagged"):
            assert all(f":{ACCOUNT}:" in arn for arn in item["Resource"]), sid


def test_a_custom_managed_tag_is_used_in_the_condition() -> None:
    policy = enforcer_policy(config("targets:\n  managed_tag: cost:stoppable\n"), ACCOUNT)
    condition = statements(policy)["Ec2InstanceTagged"]["Condition"]["StringEquals"]
    assert condition == {"aws:ResourceTag/cost:stoppable": "true"}


def test_only_enabled_types_are_granted() -> None:
    policy = enforcer_policy(config("targets:\n  types: [ec2-instance]\n"), ACCOUNT)
    assert every_action(policy) & {"rds:StopDBInstance", "ecs:UpdateService"} == set()
    assert "ec2:StopInstances" in every_action(policy)


def test_selectors_are_granted_by_explicit_arn() -> None:
    policy = enforcer_policy(
        config(
            "targets:\n  selectors:\n"
            "    - type: ec2-instance\n      ids: [i-0123456789abcdef0]\n"
            "    - type: ecs-service\n      ids: [prod/api]\n"
        ),
        ACCOUNT,
    )
    found = statements(policy)
    assert found["Ec2InstanceSelected"]["Resource"] == [
        f"arn:aws:ec2:*:{ACCOUNT}:instance/i-0123456789abcdef0"
    ]
    assert found["EcsServiceSelected"]["Resource"] == [f"arn:aws:ecs:*:{ACCOUNT}:service/prod/api"]
    assert "Condition" not in found["Ec2InstanceSelected"]


def test_the_ledger_can_be_written_but_never_pruned() -> None:
    ledger = statements(enforcer_policy(config(), ACCOUNT))["Ledger"]
    assert ledger["Action"] == ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]


def test_budgets_are_read_by_name_only() -> None:
    budgets = statements(enforcer_policy(config(), ACCOUNT))["ReadOwnBudgets"]
    assert budgets["Resource"] == [f"arn:aws:budgets::{ACCOUNT}:budget/clamper-whole-account"]


def test_the_enforcer_cannot_touch_iam_or_its_own_stack() -> None:
    """An attacker holding the role cannot widen it."""
    services = {action.split(":")[0] for action in every_action(enforcer_policy(config(), ACCOUNT))}
    assert {"iam", "cloudformation", "sts", "kms"} & services == set()


def test_the_slack_parameter_is_read_by_name() -> None:
    policy = enforcer_policy(
        config("notifications:\n  slack_webhook_parameter: /clamper/slack\n"), ACCOUNT
    )
    assert statements(policy)["SlackWebhookParameter"]["Resource"] == [
        f"arn:aws:ssm:*:{ACCOUNT}:parameter/clamper/slack"
    ]


def test_the_operator_can_see_endpoints_the_enforcer_cannot() -> None:
    enforcer = every_action(enforcer_policy(config(), ACCOUNT))
    operator = every_action(operator_policy(config(), ACCOUNT))
    assert "sagemaker:ListEndpoints" in operator
    assert "sagemaker:ListEndpoints" not in enforcer


# --- what is refused ----------------------------------------------------


@pytest.mark.parametrize("build", [enforcer_policy, operator_policy])
def test_no_destructive_verb_is_granted(build: Any) -> None:
    for action in every_action(build(config(), ACCOUNT)):
        verb = action.split(":")[1].lower()
        if action == "lambda:DeleteFunctionConcurrency":
            continue
        assert not verb.startswith(("terminate", "delete", "remove", "destroy")), action


def policy_with(statement: dict[str, Any]) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [{"Sid": "Test", **statement}]}


@pytest.mark.parametrize("action", ["*", "ec2:*", "ec2:Stop*"])
def test_a_wildcard_action_is_refused(action: str) -> None:
    with pytest.raises(PolicyError, match="wildcard"):
        validate(policy_with({"Effect": "Allow", "Action": [action], "Resource": "*"}))


@pytest.mark.parametrize(
    "action", ["ec2:TerminateInstances", "rds:DeleteDBInstance", "ec2:RebootInstances"]
)
def test_a_destructive_action_is_refused(action: str) -> None:
    with pytest.raises(PolicyError, match="destructive"):
        validate(
            policy_with(
                {"Effect": "Allow", "Action": [action], "Resource": ["arn:aws:ec2:*:1:x/*"]}
            )
        )


def test_the_named_exception_is_allowed() -> None:
    validate(
        policy_with(
            {
                "Effect": "Allow",
                "Action": ["lambda:DeleteFunctionConcurrency"],
                "Resource": ["arn:aws:lambda:*:1:function:*"],
            }
        )
    )


def test_an_unscoped_unconditioned_write_is_refused() -> None:
    with pytest.raises(PolicyError, match="no condition"):
        validate(policy_with({"Effect": "Allow", "Action": ["ec2:StopInstances"], "Resource": "*"}))


def test_a_deny_may_name_anything() -> None:
    """The rules constrain what is granted. A deny of a destructive verb is
    the opposite of a problem."""
    validate(policy_with({"Effect": "Deny", "Action": ["ec2:TerminateInstances"], "Resource": "*"}))


# --- the committed examples ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("enforcer-policy.example.json", enforcer_policy),
        ("operator-policy.example.json", operator_policy),
    ],
)
def test_the_committed_examples_match_the_generator(name: str, build: Any) -> None:
    """iam/ holds what the generator emits for the default config. If this
    fails, regenerate with `clamper policy` rather than editing by hand."""
    committed = (IAM / name).read_text(encoding="utf-8")
    assert committed == render(build(config(), ACCOUNT)), (
        f"iam/{name} is stale. Regenerate it: clamper policy --kind "
        f"{name.split('-')[0]} --account {ACCOUNT} > iam/{name}"
    )


def test_the_deploy_policy_is_valid_json_with_no_wildcard_action() -> None:
    """The deploy policy is broader by necessity and is not held to the
    reversible-only rule — CloudFormation needs delete permissions to roll a
    failed stack back. It is held to: no wildcard action, and every delete
    scoped to a clamper-named resource."""
    policy = json.loads((IAM / "deploy-policy.json").read_text(encoding="utf-8"))
    for item in policy["Statement"]:
        actions = [item["Action"]] if isinstance(item["Action"], str) else item["Action"]
        resources = [item["Resource"]] if isinstance(item["Resource"], str) else item["Resource"]
        for action in actions:
            assert action != "*" and not action.endswith(":*"), action
            if ":Delete" in action:
                assert all("clamper" in arn for arn in resources), (item["Sid"], action)
