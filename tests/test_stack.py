"""The generated CloudFormation templates.

Checked as data, after a round trip through a YAML parser, because what
CloudFormation receives is the rendered text and not the dict the generator
built. cfn-lint runs over both templates when it is installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from clamper.core.config import Config, loads
from clamper.core.policy import validate
from clamper.core.stack import bootstrap, main

BASE = (
    "version: 1\nlimits:\n"
    "  - id: whole-account\n    amount_usd: 500\n"
    "  - id: ml-team\n    amount_usd: 200\n    scope:\n      kind: tag\n"
    "      tag: team\n      value: ml\n    notification_type: forecasted\n"
    "notifications:\n  email: oncall@example.com\n"
)


def config(text: str = BASE) -> Config:
    return loads(text)


def parsed_main(cfg: Config | None = None) -> dict[str, Any]:
    loaded = yaml.safe_load(main.render_template(cfg or config()))
    assert isinstance(loaded, dict)
    return loaded


def resources_of_type(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {key: item for key, item in template["Resources"].items() if item["Type"] == kind}


def unsub(value: Any) -> Any:
    """Undo `Fn::Sub` wrapping, so the policy can be validated as text."""
    if isinstance(value, dict):
        if set(value) == {"Fn::Sub"}:
            return value["Fn::Sub"]
        return {key: unsub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unsub(item) for item in value]
    return value


# --- plain CloudFormation -----------------------------------------------


@pytest.mark.parametrize(
    "text", [lambda: main.render_template(config()), lambda: bootstrap.render_template("clamper")]
)
def test_no_transform_no_macro_no_short_form_tags(text: Any) -> None:
    """What you read is what deploys."""
    rendered = text()
    assert "Transform" not in yaml.safe_load(rendered)
    assert "!Ref" not in rendered and "!Sub" not in rendered and "!GetAtt" not in rendered


def test_no_yaml_aliases_are_emitted() -> None:
    """CloudFormation rejects `*id001` aliases outside `package`. PyYAML
    emits them for any object that appears twice, which the budget
    notifications' shared subscriber list did until cfn-lint caught it."""
    rendered = main.render_template(config())
    assert "&id0" not in rendered and "*id0" not in rendered


def test_the_rendered_template_starts_with_a_comment_header() -> None:
    assert main.render_template(config()).startswith("# Clamper main stack")


# --- budgets and topics -------------------------------------------------


def test_one_budget_per_limit_with_both_thresholds() -> None:
    budgets = resources_of_type(parsed_main(), "AWS::Budgets::Budget")
    assert set(budgets) == {"BudgetWholeAccount", "BudgetMlTeam"}
    whole = budgets["BudgetWholeAccount"]["Properties"]
    assert whole["Budget"]["BudgetName"] == "clamper-whole-account"
    assert whole["Budget"]["BudgetLimit"] == {"Amount": 500, "Unit": "USD"}
    thresholds = [
        (item["Notification"]["NotificationType"], item["Notification"]["Threshold"])
        for item in whole["NotificationsWithSubscribers"]
    ]
    assert thresholds == [("ACTUAL", 80.0), ("ACTUAL", 100.0)]


def test_a_tag_scoped_limit_filters_its_budget() -> None:
    budget = parsed_main()["Resources"]["BudgetMlTeam"]["Properties"]
    assert budget["Budget"]["CostFilters"] == {"TagKeyValue": ["user:team$ml"]}
    kinds = [
        item["Notification"]["NotificationType"] for item in budget["NotificationsWithSubscribers"]
    ]
    assert kinds == ["ACTUAL", "FORECASTED"]


def test_equal_thresholds_do_not_produce_a_duplicate_notification() -> None:
    cfg = config(
        "version: 1\nlimits:\n  - id: a\n    amount_usd: 5\n"
        "    warn_percent: 100\n    clamp_percent: 100\n"
    )
    budget = parsed_main(cfg)["Resources"]["BudgetA"]["Properties"]
    assert len(budget["NotificationsWithSubscribers"]) == 1


def test_budgets_publish_to_the_alerts_topic_not_the_notifications_topic() -> None:
    """The enforcer subscribes to the alerts topic. If it published there,
    every notification would trigger another reconcile."""
    template = parsed_main()
    for budget in resources_of_type(template, "AWS::Budgets::Budget").values():
        for item in budget["Properties"]["NotificationsWithSubscribers"]:
            assert item["Subscribers"] == [
                {"SubscriptionType": "SNS", "Address": {"Ref": "AlertsTopic"}}
            ]
    sub = template["Resources"]["EnforcerAlertsSubscription"]["Properties"]
    assert sub["TopicArn"] == {"Ref": "AlertsTopic"}
    env = template["Resources"]["Enforcer"]["Properties"]["Environment"]["Variables"]
    assert env["CLAMPER_TOPIC_ARN"] == {"Ref": "NotificationsTopic"}


def test_the_alerts_topic_policy_has_both_confused_deputy_conditions() -> None:
    statement = parsed_main()["Resources"]["AlertsTopicPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ][0]
    assert statement["Principal"] == {"Service": "budgets.amazonaws.com"}
    assert statement["Condition"]["StringEquals"] == {
        "aws:SourceAccount": {"Ref": "AWS::AccountId"}
    }
    assert "aws:SourceArn" in statement["Condition"]["ArnLike"]


def test_the_email_subscription_is_present_only_when_configured() -> None:
    assert "NotificationsEmail" in parsed_main()["Resources"]
    cfg = config("version: 1\nlimits:\n  - id: a\n    amount_usd: 5\n")
    assert "NotificationsEmail" not in parsed_main(cfg)["Resources"]


# --- the ledger ---------------------------------------------------------


def test_the_ledger_is_retained_recoverable_and_never_expires() -> None:
    ledger = parsed_main()["Resources"]["Ledger"]
    assert ledger["DeletionPolicy"] == "Retain"
    assert ledger["UpdateReplacePolicy"] == "Retain"
    properties = ledger["Properties"]
    assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert "TimeToLiveSpecification" not in properties
    assert properties["GlobalSecondaryIndexes"][0]["IndexName"] == "by-status"


# --- the enforcer -------------------------------------------------------


def test_the_role_trusts_lambda_in_this_account_only() -> None:
    trust = parsed_main()["Resources"]["EnforcerRole"]["Properties"]["AssumeRolePolicyDocument"]
    (statement,) = trust["Statement"]
    assert statement["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_the_role_policy_passes_the_same_guardrails() -> None:
    """The policy inside the template is held to the same rules as the one
    `clamper policy` prints — it is the one that actually runs."""
    role = parsed_main()["Resources"]["EnforcerRole"]["Properties"]
    (policy,) = role["Policies"]
    validate(unsub(policy["PolicyDocument"]))
    assert "ManagedPolicyArns" not in role


def test_logs_are_kept_thirty_days_and_exist_before_the_function() -> None:
    template = parsed_main()
    assert template["Resources"]["EnforcerLogs"]["Properties"]["RetentionInDays"] == 30
    assert template["Resources"]["Enforcer"]["DependsOn"] == ["EnforcerLogs"]


def test_the_schedule_follows_the_config() -> None:
    cfg = config(
        "version: 1\nlimits:\n  - id: a\n    amount_usd: 5\nsafety:\n  reconcile_hours: 1\n"
    )
    rule = parsed_main(cfg)["Resources"]["ReconcileSchedule"]["Properties"]
    assert rule["ScheduleExpression"] == "rate(1 hour)"
    assert parsed_main()["Resources"]["ReconcileSchedule"]["Properties"]["ScheduleExpression"] == (
        "rate(6 hours)"
    )


def test_the_enforcer_is_told_its_own_resources() -> None:
    """Self-exclusion check two, without cloudformation permission."""
    env = parsed_main()["Resources"]["Enforcer"]["Properties"]["Environment"]["Variables"]
    joined = env["CLAMPER_STACK_RESOURCES"]["Fn::Join"][1]
    assert "clamper-enforcer" in joined
    assert {"Ref": "Ledger"} in joined
    assert env["CLAMPER_STACK_NAME"] == "clamper"


def test_no_webhook_url_or_secret_appears_in_the_template() -> None:
    cfg = config(
        "version: 1\nlimits:\n  - id: a\n    amount_usd: 5\n"
        "notifications:\n  slack_webhook_parameter: /clamper/slack\n"
    )
    rendered = main.render_template(cfg)
    assert "hooks.slack.com" not in rendered
    assert "/clamper/slack" in rendered  # the parameter *name*, in the policy


# --- the bootstrap stack ------------------------------------------------


def test_the_bucket_is_private_encrypted_versioned_and_tls_only() -> None:
    template = yaml.safe_load(bootstrap.render_template("clamper"))
    bucket = template["Resources"]["ArtifactBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    rule = bucket["LifecycleConfiguration"]["Rules"][0]
    assert rule["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30}
    policy = template["Resources"]["ArtifactBucketPolicy"]["Properties"]["PolicyDocument"]
    assert policy["Statement"][0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


# --- cfn-lint, when available -------------------------------------------


@pytest.mark.skipif(shutil.which("cfn-lint") is None, reason="cfn-lint is not installed")
def test_cfn_lint_passes(tmp_path: Path) -> None:
    (tmp_path / "main.yaml").write_text(main.render_template(config()), encoding="utf-8")
    (tmp_path / "bootstrap.yaml").write_text(bootstrap.render_template("clamper"), encoding="utf-8")
    result = subprocess.run(
        ["cfn-lint", str(tmp_path / "main.yaml"), str(tmp_path / "bootstrap.yaml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
