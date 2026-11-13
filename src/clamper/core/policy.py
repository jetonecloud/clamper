"""Least privilege, generated from `actions.yml` rather than written by hand.

Two policies come out of here:

- **The enforcer's**, attached to the Lambda's role by the stack. Discovery
  reads for the enabled types; each write scoped to its resource ARN pattern
  and conditioned on the managed tag with that action's *own* condition key;
  the ledger table, the topic, the budgets and the log group, by ARN.
- **The operator's**, for a person running `plan`, `status`, `release` and
  `clamp`. The same writes, plus the reads those commands make that the
  enforcer never does.

The deploy policy — what `clamper deploy` needs — is broader, used once, and
lives in `iam/deploy-policy.json` with its own account of what it can do.

There is no second list of permissions to keep in step with the code. That
is the point: a least-privilege policy maintained separately from the code it
permits is how it rots into a wildcard.

**No IAM deny on the protect tag.** It looks like a free second layer and is
not: for ECS, Auto Scaling and Lambda the clamp and the release are the same
IAM action, so a deny would also block *releasing* anything somebody tagged
`clamper:protect` after it was clamped — turning the safety tag into a way to
make a clamp unreleasable. The protect check is the runtime one in
`core/targets.py`, which nothing overrides.

**What `validate()` refuses**, for both policies and for the policy inside
the generated template:

- `*` as an action, or `service:*`;
- any action whose verb is a terminate, delete or other destructive verb from
  `actions.yml`, bar the single named `lambda:DeleteFunctionConcurrency`;
- an `Allow` of a write action on `Resource: "*"` with no condition.

Preflight refuses to emit a policy containing a write verb. Clamper has to
write, so the rule is inverted rather than dropped: no destructive verb, and
no unscoped write.

Importable inside a Lambda (the template generator uses it), though the
Lambda never calls it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from clamper.core.actions import READ_PREFIXES, Action, Registry, load_registry
from clamper.core.config import Config
from clamper.core.names import StackNames

POLICY_VERSION = "2012-10-17"

#: IAM verbs that only read. `View` is how Budgets spells it:
#: `budgets:ViewBudget` is the permission behind the `DescribeBudget` API.
READ_VERBS = (*READ_PREFIXES, "View", "Query", "BatchGet")

#: Reads `clamper plan` makes that the enforcer never does. SageMaker
#: endpoints are found and priced so the biggest number stays on the page,
#: though v1 will not touch them.
VISIBILITY_READS = (
    "sagemaker:ListEndpoints",
    "sagemaker:DescribeEndpoint",
    "sagemaker:DescribeEndpointConfig",
)

#: Reads the CLI makes about Clamper itself, and `doctor`'s checks.
OPERATOR_READS = (
    "sts:GetCallerIdentity",
    "ce:ListCostAllocationTags",
)


class PolicyError(ValueError):
    """A generated policy broke one of the rules above.

    Raised rather than logged: a policy that breaks them must not be written
    to a file or a template, and the only way to make sure is to not return
    it.
    """


def enforcer_policy(
    config: Config, account: str, *, registry: Registry | None = None, region: str = "*"
) -> dict[str, Any]:
    """The policy the enforcer runs as."""
    registry = registry if registry is not None else load_registry()
    actions = registry.enabled(config.targets.types)
    names = StackNames(config.stack_name)
    statements: list[dict[str, Any]] = [
        _discover(actions),
        *_writes(actions, config, account),
        _budgets(config, account),
        _ledger(names, account, region, read_only=False),
        {
            "Sid": "Notify",
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": [f"arn:aws:sns:{region}:{account}:{names.topic}"],
        },
        {
            "Sid": "OwnLogs",
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [f"arn:aws:logs:{region}:{account}:log-group:{names.log_group}:*"],
        },
    ]
    if config.notifications.slack_webhook_parameter:
        statements.append(
            {
                "Sid": "SlackWebhookParameter",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter"],
                "Resource": [
                    f"arn:aws:ssm:{region}:{account}:parameter"
                    f"{config.notifications.slack_webhook_parameter}"
                ],
            }
        )
    policy = {"Version": POLICY_VERSION, "Statement": statements}
    validate(policy, registry=registry)
    return policy


def operator_policy(
    config: Config, account: str, *, registry: Registry | None = None, region: str = "*"
) -> dict[str, Any]:
    """The policy for a person running plan, status, release and clamp."""
    registry = registry if registry is not None else load_registry()
    actions = registry.enabled(config.targets.types)
    names = StackNames(config.stack_name)
    statements: list[dict[str, Any]] = [
        _discover(actions, extra=VISIBILITY_READS + OPERATOR_READS),
        *_writes(actions, config, account),
        _budgets(config, account),
        _ledger(names, account, region, read_only=False),
        {
            "Sid": "OwnStack",
            "Effect": "Allow",
            "Action": [
                "cloudformation:DescribeStacks",
                "cloudformation:ListStackResources",
            ],
            "Resource": [f"arn:aws:cloudformation:{region}:{account}:stack/{config.stack_name}/*"],
        },
        {
            "Sid": "OwnTopic",
            "Effect": "Allow",
            "Action": ["sns:ListSubscriptionsByTopic"],
            "Resource": [f"arn:aws:sns:{region}:{account}:{names.topic}"],
        },
    ]
    policy = {"Version": POLICY_VERSION, "Statement": statements}
    validate(policy, registry=registry)
    return policy


# --- statements ---------------------------------------------------------


def _discover(actions: Iterable[Action], extra: Iterable[str] = ()) -> dict[str, Any]:
    """Discovery is enumeration, so it cannot be resource-scoped. Reads only."""
    found = sorted({item for action in actions for item in action.discover} | set(extra))
    return {"Sid": "Discover", "Effect": "Allow", "Action": found, "Resource": "*"}


def _writes(actions: Iterable[Action], config: Config, account: str) -> list[dict[str, Any]]:
    """Each action's clamp and release, scoped to its ARN and tag.

    Opt-in by tag gets a tag condition with the action's own key — Auto
    Scaling's is `autoscaling:ResourceTag/`, and the general key on that
    action silently never matches. Opt-in by selector, or by acknowledged
    `include_untagged`, cannot be expressed as a tag condition, so those
    types get a second statement: explicit resource ARNs for selectors, and
    the account-scoped ARN pattern for `include_untagged`. Never `*`.
    """
    targets = config.targets
    statements: list[dict[str, Any]] = []
    for action in actions:
        writes = sorted({operation.iam for operation in action.operations})
        base = _sid(action.id)
        statement: dict[str, Any] = {
            "Sid": f"{base}Tagged",
            "Effect": "Allow",
            "Action": writes,
            "Resource": [action.arn(account)],
        }
        if action.tag_condition is not None:
            statement["Condition"] = {
                "StringEquals": {
                    f"{action.tag_condition}{targets.managed_tag}": targets.managed_tag_value
                }
            }
        statements.append(statement)

        if targets.include_untagged:
            statements.append(
                {
                    "Sid": f"{base}Untagged",
                    "Effect": "Allow",
                    "Action": writes,
                    "Resource": [action.arn(account)],
                }
            )
        elif ids := sorted(targets.ids_for(action.id)):
            statements.append(
                {
                    "Sid": f"{base}Selected",
                    "Effect": "Allow",
                    "Action": writes,
                    "Resource": [_selector_arn(action, account, item) for item in ids],
                }
            )
    return statements


def _budgets(config: Config, account: str) -> dict[str, Any]:
    return {
        "Sid": "ReadOwnBudgets",
        "Effect": "Allow",
        # The permission behind the DescribeBudget API.
        "Action": ["budgets:ViewBudget"],
        "Resource": [
            f"arn:aws:budgets::{account}:budget/{limit.budget_name(config.stack_name)}"
            for limit in config.limits
        ],
    }


def _ledger(names: StackNames, account: str, region: str, *, read_only: bool) -> dict[str, Any]:
    table = f"arn:aws:dynamodb:{region}:{account}:table/{names.table}"
    actions = ["dynamodb:GetItem", "dynamodb:Query"]
    if not read_only:
        # PutItem only. No DeleteItem, no UpdateTimeToLive: the ledger is
        # never pruned, and that is a property of the permission as well as
        # of the code.
        actions.append("dynamodb:PutItem")
    return {
        "Sid": "Ledger",
        "Effect": "Allow",
        "Action": sorted(actions),
        "Resource": [table, f"{table}/index/*"],
    }


def _selector_arn(action: Action, account: str, resource_id: str) -> str:
    """The ARN for one explicitly selected resource.

    The action's pattern with its trailing wildcard replaced by the id. Every
    shipped pattern ends in `*`; one that did not would need its own rule,
    and raising here makes that a failure rather than a malformed ARN.
    """
    pattern = action.arn(account)
    if not pattern.endswith("*"):
        raise PolicyError(f"{action.id}'s arn_pattern does not end in a wildcard: {pattern}")
    if action.id == "ecs-service":
        # `<cluster>/<service>` is already the tail of an ECS service ARN.
        return pattern[: -len("*/*")] + resource_id
    return pattern[:-1] + resource_id


def _sid(action_id: str) -> str:
    return "".join(part.capitalize() for part in action_id.split("-"))


# --- validation ---------------------------------------------------------


def validate(policy: dict[str, Any], *, registry: Registry | None = None) -> None:
    """Refuse a policy that breaks the rules in the module docstring."""
    registry = registry if registry is not None else load_registry()
    verbs = registry.destructive_verbs
    exceptions = registry.destructive_verb_exceptions
    for statement in policy.get("Statement", []):
        sid = statement.get("Sid", "(no sid)")
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else list(actions)
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else list(resources)
        allow = statement.get("Effect") == "Allow"

        for action in actions:
            if action == "*" or action.endswith(":*"):
                raise PolicyError(f"{sid}: {action!r} is a wildcard action.")
            service, _, verb = action.partition(":")
            if not verb:
                raise PolicyError(f"{sid}: {action!r} is not service:Action.")
            if "*" in verb and allow:
                raise PolicyError(f"{sid}: {action!r} is a wildcard action.")
            if allow and action not in exceptions:
                for bad in verbs:
                    if verb.lower().startswith(bad):
                        raise PolicyError(
                            f"{sid}: {action} has the destructive verb {bad!r}. Clamper "
                            f"is reversible-only; its policy may not grant this."
                        )
            if allow and not _is_read(verb) and "*" in resources and not statement.get("Condition"):
                raise PolicyError(
                    f'{sid}: {action} is a write on Resource "*" with no condition. '
                    f"Every write Clamper is granted is scoped to an ARN pattern."
                )


def _is_read(verb: str) -> bool:
    return verb.startswith(READ_VERBS)


def render(policy: dict[str, Any]) -> str:
    """Stable JSON: sorted keys off (statement order is meaningful), two
    spaces, trailing newline — so a regenerated file diffs cleanly."""
    return json.dumps(policy, indent=2) + "\n"
