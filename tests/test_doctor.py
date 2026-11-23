"""`clamper doctor`: each check, against botocore's Stubber.

Stubber rather than moto because moto's CloudFormation and Cost Explorer
need optional dependencies, and a Stubber validates every request against
the real service model anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from support import ACCOUNT, REGION

from clamper.core import doctor
from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit, loads
from clamper.core.session import Clients

TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:clamper-notifications"


def config(extra: str = "") -> Config:
    return loads(
        "version: 1\nlimits:\n  - id: whole-account\n    amount_usd: 100\n"
        "notifications:\n  email: ops@example.com\n" + extra
    )


@pytest.fixture()
def stubbed() -> Iterator[tuple[Clients, dict[str, Stubber]]]:
    session = boto3.Session(region_name=REGION)
    clients = Clients(session, default_region=REGION)
    stubs: dict[str, Stubber] = {}
    for service in ("cloudformation", "sns", "ce", "events", "lambda"):
        client = session.client(service, region_name=REGION)
        clients.stub(service, client)
        stubs[service] = Stubber(client)
        stubs[service].activate()
    yield clients, stubs
    for stub in stubs.values():
        stub.deactivate()


def test_a_missing_stack_is_a_fail_with_the_fix(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["cloudformation"].add_client_error(
        "describe_stacks",
        service_error_code="ValidationError",
        service_message="Stack with id clamper does not exist",
    )
    check = doctor.stack_check(config(), clients)
    assert check.status == doctor.FAIL
    assert "clamper deploy" in check.detail


def test_a_settled_stack_passes(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["cloudformation"].add_response(
        "describe_stacks",
        {
            "Stacks": [
                {
                    "StackName": "clamper",
                    "CreationTime": "2026-11-13T00:00:00Z",
                    "StackStatus": "UPDATE_COMPLETE",
                }
            ]
        },
        {"StackName": "clamper"},
    )
    assert doctor.stack_check(config(), clients).status == doctor.PASS


def _subscription(arn: str) -> dict[str, Any]:
    return {
        "Subscriptions": [
            {
                "SubscriptionArn": arn,
                "Owner": ACCOUNT,
                "Protocol": "email",
                "Endpoint": "ops@example.com",
                "TopicArn": TOPIC,
            }
        ]
    }


def test_an_unconfirmed_subscription_is_a_fail_not_a_warning(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["sns"].add_response(
        "list_subscriptions_by_topic", _subscription("PendingConfirmation"), {"TopicArn": TOPIC}
    )
    check = doctor.subscription_check(config(), clients, ACCOUNT)
    assert check.status == doctor.FAIL
    assert "click" in check.detail


def test_a_confirmed_subscription_passes(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["sns"].add_response(
        "list_subscriptions_by_topic",
        _subscription(f"{TOPIC}:0f1e2d3c-aaaa-bbbb-cccc-000000000000"),
        {"TopicArn": TOPIC},
    )
    assert doctor.subscription_check(config(), clients, ACCOUNT).status == doctor.PASS


def test_no_notification_at_all_is_a_fail(stubbed: Any) -> None:
    clients, _ = stubbed
    bare = loads("version: 1\nlimits:\n  - id: whole-account\n    amount_usd: 100\n")
    assert doctor.subscription_check(bare, clients, ACCOUNT).status == doctor.FAIL


TAGGED = (
    "  - id: team-ml\n    amount_usd: 50\n    scope:\n      kind: tag\n"
    "      tag: team\n      value: ml\n"
)


def tagged_config() -> Config:
    return loads("version: 1\nlimits:\n  - id: whole-account\n    amount_usd: 100\n" + TAGGED)


def test_an_inactive_cost_allocation_tag_is_a_fail(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["ce"].add_response(
        "list_cost_allocation_tags",
        {"CostAllocationTags": [{"TagKey": "team", "Type": "UserDefined", "Status": "Inactive"}]},
        {"TagKeys": ["team"]},
    )
    (check,) = doctor.cost_tag_checks(tagged_config(), clients)
    assert check.status == doctor.FAIL
    assert "$0.00" in check.detail


def test_an_active_cost_allocation_tag_passes(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["ce"].add_response(
        "list_cost_allocation_tags",
        {"CostAllocationTags": [{"TagKey": "team", "Type": "UserDefined", "Status": "Active"}]},
        {"TagKeys": ["team"]},
    )
    (check,) = doctor.cost_tag_checks(tagged_config(), clients)
    assert check.status == doctor.PASS


def test_whole_account_limits_need_no_tag_check(stubbed: Any) -> None:
    clients, _ = stubbed
    assert doctor.cost_tag_checks(config(), clients) == []


def test_an_unreadable_budget_fails_and_a_readable_one_passes() -> None:
    def read(limit: Limit) -> Spend:
        if limit.id == "whole-account":
            return Spend(limit=limit, budget_name="b", actual=12.0, budget_amount=100.0)
        return Spend(limit=limit, budget_name="b", error="budget does not exist")

    checks = doctor.budget_checks(tagged_config(), read)
    assert [check.status for check in checks] == [doctor.PASS, doctor.FAIL]
    assert doctor.exit_code(checks) == 2
    assert doctor.exit_code(checks[:1]) == 0


def test_doctor_reports_missing_credentials_instead_of_crashing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from clamper import cli
    from clamper.core import session as session_module

    def no_credentials(session: Any) -> Any:
        raise session_module.CredentialsError("No usable AWS credentials found.")

    monkeypatch.setattr(session_module, "whoami", no_credentials)
    path = tmp_path / "clamper.yml"
    path.write_text("version: 1\nlimits:\n  - id: a\n    amount_usd: 5\n", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["doctor", "--config", str(path)])
    assert result.exit_code == 2
    assert "FAIL  credentials" in result.stdout


# --- the reconcile schedule ------------------------------------------------


def _rule(state: str) -> dict[str, Any]:
    return {
        "Name": "clamper-reconcile",
        "Arn": f"arn:aws:events:{REGION}:{ACCOUNT}:rule/clamper-reconcile",
        "State": state,
        "ScheduleExpression": "rate(6 hours)",
    }


def test_an_enabled_schedule_passes(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["events"].add_response("describe_rule", _rule("ENABLED"), {"Name": "clamper-reconcile"})
    check = doctor.schedule_check(config(), clients)
    assert check.status == doctor.PASS
    assert "rate(6 hours)" in check.detail


def test_a_disabled_schedule_is_a_fail_not_a_warning(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["events"].add_response("describe_rule", _rule("DISABLED"), {"Name": "clamper-reconcile"})
    check = doctor.schedule_check(config(), clients)
    assert check.status == doctor.FAIL
    assert "DISABLED" in check.detail


def test_a_missing_schedule_is_a_fail_with_the_fix(stubbed: Any) -> None:
    clients, stubs = stubbed
    stubs["events"].add_client_error(
        "describe_rule",
        service_error_code="ResourceNotFoundException",
        service_message="Rule clamper-reconcile does not exist.",
    )
    check = doctor.schedule_check(config(), clients)
    assert check.status == doctor.FAIL
    assert "clamper deploy" in check.detail
