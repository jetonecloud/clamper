"""`clamper doctor`: is everything in place for a clamp to work and be heard?

Each check is a read, and each one is here because its failure is silent in
practice — the setup looks fine and the first anybody learns of the problem
is a clamp that never happened or a notification nobody got:

1. **Credentials** work, and are not the account root user's.
2. **The stack exists** and is in a settled state.
3. **The email subscription is confirmed.** AWS sends a confirmation link and
   delivers nothing until a human clicks it. `PendingConfirmation` is a
   FAIL, not a warning: an unconfirmed subscription is a clamp nobody hears
   about.
4. **Cost allocation tags are active** for every tag-scoped limit. An
   inactive tag makes a tag-scoped budget read $0.00 forever, which looks
   exactly like nothing being wrong. Checked through
   `ce:ListCostAllocationTags`, which is free; nothing here calls
   `GetCostAndUsage`.
5. **Every budget is readable** and has calculated spend.
6. **The reconcile schedule is enabled.** A disabled rule leaves the
   once-a-period Budgets alert as the only thing that wakes the enforcer,
   and nothing anywhere says so.

Importable inside a Lambda, though only the CLI uses it. The credentials
check lives in the CLI, because it decides whether the rest can run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.budgets import Spend
from clamper.core.config import SCOPE_TAG, Config, Limit
from clamper.core.names import StackNames
from clamper.core.session import Clients

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

#: CloudFormation states in which the stack is settled and working.
SETTLED = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"})


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def line(self) -> str:
        return f"  {self.status}  {self.name:<24} {self.detail}"


def run_checks(
    config: Config,
    clients: Clients,
    *,
    account: str,
    read_spend: Callable[[Limit], Spend],
) -> list[Check]:
    checks = [stack_check(config, clients)]
    checks.append(subscription_check(config, clients, account))
    checks.extend(cost_tag_checks(config, clients))
    checks.extend(budget_checks(config, read_spend))
    checks.append(schedule_check(config, clients))
    return checks


def stack_check(config: Config, clients: Clients) -> Check:
    client = clients.get("cloudformation")
    try:
        stacks = client.describe_stacks(StackName=config.stack_name).get("Stacks", [])
    except ClientError as exc:
        code, message = _error(exc)
        if code == "ValidationError" and "does not exist" in message:
            return Check(
                "stack",
                FAIL,
                f"{config.stack_name} does not exist in {clients.default_region}. "
                f"Run `clamper deploy`.",
            )
        return Check("stack", FAIL, f"could not read {config.stack_name}: {code} - {message}")
    except BotoCoreError as exc:
        return Check("stack", FAIL, f"could not read {config.stack_name}: {exc}")
    status = str(stacks[0].get("StackStatus") if stacks else "MISSING")
    if status in SETTLED:
        return Check("stack", PASS, f"{config.stack_name} is {status}")
    return Check(
        "stack",
        WARN if status.endswith("_IN_PROGRESS") else FAIL,
        f"{config.stack_name} is {status}",
    )


def subscription_check(config: Config, clients: Clients, account: str) -> Check:
    email = config.notifications.email
    if not email:
        if config.notifications.slack_webhook_parameter:
            return Check("email subscription", WARN, "no email set; Slack only")
        return Check(
            "email subscription",
            FAIL,
            "no email or Slack is configured. Nobody will hear about a clamp.",
        )
    region = clients.default_region or ""
    topic = f"arn:aws:sns:{region}:{account}:{StackNames(config.stack_name).topic}"
    client = clients.get("sns")
    found: list[str] = []
    try:
        paginator = client.get_paginator("list_subscriptions_by_topic")
        for page in paginator.paginate(TopicArn=topic):
            for item in page.get("Subscriptions", []):
                if str(item.get("Endpoint", "")).lower() == email.lower():
                    found.append(str(item.get("SubscriptionArn", "")))
    except ClientError as exc:
        code, message = _error(exc)
        return Check("email subscription", FAIL, f"could not read {topic}: {code} - {message}")
    except BotoCoreError as exc:
        return Check("email subscription", FAIL, f"could not read {topic}: {exc}")
    if not found:
        return Check(
            "email subscription",
            FAIL,
            f"{email} is not subscribed to {topic}. Run `clamper deploy` again.",
        )
    if all(arn == "PendingConfirmation" for arn in found):
        return Check(
            "email subscription",
            FAIL,
            f"{email} has not confirmed. Find the email from AWS Notifications and click "
            f"the link; nothing is delivered until somebody does.",
        )
    return Check("email subscription", PASS, f"{email} is confirmed")


def cost_tag_checks(config: Config, clients: Clients) -> list[Check]:
    keys = sorted(
        {
            limit.scope.tag
            for limit in config.limits
            if limit.scope.kind == SCOPE_TAG and limit.scope.tag
        }
    )
    if not keys:
        return []
    client = clients.get("ce")
    try:
        response = client.list_cost_allocation_tags(TagKeys=keys)
    except ClientError as exc:
        code, message = _error(exc)
        return [Check("cost allocation tags", FAIL, f"could not read: {code} - {message}")]
    except BotoCoreError as exc:
        return [Check("cost allocation tags", FAIL, f"could not read: {exc}")]
    status = {
        str(item.get("TagKey")): str(item.get("Status"))
        for item in response.get("CostAllocationTags", [])
    }
    out: list[Check] = []
    for key in keys:
        state = status.get(key)
        if state == "Active":
            out.append(Check(f"tag {key}", PASS, "active as a cost allocation tag"))
        else:
            out.append(
                Check(
                    f"tag {key}",
                    FAIL,
                    f"{'inactive' if state else 'not found'} as a cost allocation tag. "
                    f"A budget filtered on it reads $0.00. Activate it in Billing > Cost "
                    f"allocation tags; AWS applies it only to usage from then on.",
                )
            )
    return out


def budget_checks(config: Config, read_spend: Callable[[Limit], Spend]) -> list[Check]:
    out: list[Check] = []
    for limit in config.limits:
        spend = read_spend(limit)
        name = f"budget {limit.id}"
        if spend.error:
            out.append(Check(name, FAIL, spend.error))
        elif spend.mismatch:
            out.append(Check(name, WARN, spend.mismatch))
        else:
            out.append(
                Check(
                    name,
                    PASS,
                    f"${spend.actual or 0:,.2f} of ${limit.amount_usd:,.2f} this period",
                )
            )
    return out


def schedule_check(config: Config, clients: Clients) -> Check:
    rule = StackNames(config.stack_name).schedule
    client = clients.get("events")
    try:
        response = client.describe_rule(Name=rule)
    except ClientError as exc:
        code, message = _error(exc)
        if code == "ResourceNotFoundException":
            return Check(
                "reconcile schedule",
                FAIL,
                f"{rule} does not exist. Run `clamper deploy` again.",
            )
        return Check("reconcile schedule", FAIL, f"could not read {rule}: {code} - {message}")
    except BotoCoreError as exc:
        return Check("reconcile schedule", FAIL, f"could not read {rule}: {exc}")
    state = str(response.get("State") or "UNKNOWN")
    if state == "ENABLED":
        expression = response.get("ScheduleExpression") or "no schedule expression"
        return Check("reconcile schedule", PASS, f"{rule} is enabled, {expression}")
    return Check(
        "reconcile schedule",
        FAIL,
        f"{rule} is {state}. Only a Budgets alert, once a period, will wake the enforcer. "
        f"Enable the rule, or run `clamper deploy` again.",
    )


def exit_code(checks: list[Check]) -> int:
    return 2 if any(check.status == FAIL for check in checks) else 0


def _error(exc: ClientError) -> tuple[str, str]:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "ClientError"), str(error.get("Message") or "")
