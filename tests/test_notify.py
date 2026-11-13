"""The notification, and the Lambda entry point that sends it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import ACCOUNT, REGION
from test_resource_ec2 import launch

from clamper.core.budgets import Spend
from clamper.core.config import Limit
from clamper.core.report import model, notification
from clamper.enforcer import handler as handler_module
from clamper.enforcer import notify
from clamper.enforcer.run import TRIGGER_ALERT, TRIGGER_MANUAL, TRIGGER_SCHEDULE

LIMIT = model.LimitLine(
    id="whole-account",
    amount_usd=100.0,
    scope="whole account",
    level="clamp",
    actual_usd=150.0,
    forecast_usd=None,
    percent=150.0,
    basis="actual",
    reason="actual spend $150.00 is 150% of the $100.00 limit; clamp threshold is 100%",
    warn_percent=80.0,
    clamp_percent=100.0,
)


def line(status: str, target: str = "i-0123456789abcdef0", detail: str = "") -> model.Line:
    return model.Line(
        status=status,
        action_id="ec2-instance",
        title="EC2 instance",
        target_id=target,
        region=REGION,
        description="p4d.24xlarge, running",
        detail=detail,
        hourly_usd=32.7726,
        clamp_id="20261113t153000z-a1b2c3",
    )


def report(**changes: Any) -> model.Report:
    base: dict[str, Any] = {
        "kind": model.KIND_RUN,
        "stack_name": "clamper",
        "mode": "enforce",
        "actor": "enforcer:alert",
        "limits": (LIMIT,),
        "acted": (line(model.CLAMPED),),
        "not_acted": (line(model.FAILED, "i-0fedcba9876543210", "IncorrectInstanceState: no"),),
        "clamp_id": "20261113t153000z-a1b2c3",
    }
    base.update(changes)
    return model.Report(**base)


# --- the text -----------------------------------------------------------


def test_the_five_things_appear_in_order() -> None:
    """What crossed, which limit, what stopped, what did not and why, and
    how to undo it."""
    text = notification.body(report())
    positions = [
        text.index("150%"),
        text.index("Limit whole-account"),
        text.index("STOPPED (1)"),
        text.index("NOT STOPPED (1)"),
        text.index("clamper release 20261113t153000z-a1b2c3"),
    ]
    assert positions[1] <= positions[0]  # the limit line carries the figure
    assert positions[1:] == sorted(positions[1:])


def test_a_failure_is_named_in_the_subject() -> None:
    assert "1 NOT stopped" in notification.subject(report())


def test_a_dry_run_says_nothing_was_stopped() -> None:
    text = notification.body(report(mode="dry-run", acted=(line(model.WOULD_CLAMP),), not_acted=()))
    assert "NOTHING WAS STOPPED" in text
    assert "STOPPED (" not in text.replace("NOT STOPPED", "")
    assert "would stop" in notification.subject(
        report(mode="dry-run", acted=(line(model.WOULD_CLAMP),), not_acted=())
    )
    # No release command for a run that stopped nothing.
    assert "clamper release" not in text


def test_control_characters_never_reach_the_message() -> None:
    hostile = line(model.FAILED, "i-0fedcba9876543210", "bad\x1b[31m\r\nInjected: header\x07")
    text = notification.body(report(not_acted=(hostile,)))
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert "\nInjected" not in text


def test_the_subject_fits_sns() -> None:
    assert len(notification.subject(report(stack_name="x" * 200))) <= 100


def test_long_sections_are_cut_before_the_release_command() -> None:
    many = tuple(line(model.CLAMPED, f"i-{n:017x}") for n in range(60))
    text = notification.body(report(acted=many))
    assert "and 35 more" in text
    assert text.rstrip().endswith("not a bill.")
    assert "clamper release" in text


# --- sending ------------------------------------------------------------


def test_it_publishes_to_the_topic(clients_moto: Any, moto_sns: Any) -> None:
    topic = moto_sns.create_topic(Name="clamper-notifications")["TopicArn"]
    delivered = notify.send(report(), clients=clients_moto, topic_arn=topic)
    assert delivered.sns is True
    assert delivered.slack is None


def test_a_send_failure_is_returned_not_raised(clients_moto: Any) -> None:
    delivered = notify.send(
        report(), clients=clients_moto, topic_arn=f"arn:aws:sns:{REGION}:{ACCOUNT}:nope"
    )
    assert delivered.sns is False
    assert delivered.errors


@pytest.mark.parametrize(
    "url", ["http://hooks.slack.com/services/x", "https://evil.example.com/hook", "not a url"]
)
def test_a_webhook_that_is_not_slack_over_https_is_refused(
    clients_moto: Any, aws_client: Any, moto_sns: Any, url: str
) -> None:
    """A parameter that could point anywhere would make the enforcer a way to
    send resource ids and spend to any URL."""
    aws_client("ssm").put_parameter(Name="/clamper/slack", Value=url, Type="SecureString")
    topic = moto_sns.create_topic(Name="clamper-notifications")["TopicArn"]
    delivered = notify.send(
        report(), clients=clients_moto, topic_arn=topic, slack_parameter="/clamper/slack"
    )
    assert delivered.slack is False
    assert delivered.errors == ("slack: ValueError",)
    # The URL is never echoed.
    assert all(url not in error for error in delivered.errors)


# --- the entry point ----------------------------------------------------


@pytest.mark.parametrize(
    ("event", "trigger"),
    [
        ({"Records": [{"EventSource": "aws:sns", "Sns": {"Message": "{...}"}}]}, TRIGGER_ALERT),
        ({"source": "clamper.schedule"}, TRIGGER_SCHEDULE),
        ({}, TRIGGER_MANUAL),
        ("not even a mapping", TRIGGER_MANUAL),
    ],
)
def test_the_trigger_is_all_that_is_read_from_the_event(event: Any, trigger: str) -> None:
    assert handler_module.trigger_of(event) == trigger


class _Context:
    invoked_function_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:clamper-enforcer"


def test_the_account_comes_from_the_function_arn_not_sts() -> None:
    assert handler_module.account_of(_Context()) == ACCOUNT


def test_the_handler_end_to_end(
    clients_moto: Any,
    moto_ec2: Any,
    moto_sns: Any,
    dynamodb_table: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spoofed SNS message claims a huge breach; the real spend is over
    the limit too, so the clamp happens — because of the spend, not the
    message."""
    instance = launch(moto_ec2, tags={"clamper:managed": "true"})
    topic = moto_sns.create_topic(Name="clamper-notifications")["TopicArn"]
    config = tmp_path / "clamper.yml"
    config.write_text(
        f"version: 1\nmode: enforce\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(handler_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(handler_module.TOPIC_ENV, topic)
    monkeypatch.setenv("CLAMPER_LEDGER_TABLE", dynamodb_table)
    monkeypatch.setenv("CLAMPER_STACK_NAME", "clamper")
    monkeypatch.setenv("CLAMPER_STACK_RESOURCES", "clamper-enforcer")

    seen: list[str] = []

    def fake_read(clients: Any, account: str, limit: Limit, stack: str) -> Spend:
        seen.append(account)
        return Spend(limit=limit, budget_name="clamper-whole-account", actual=150.0)

    monkeypatch.setattr(handler_module, "read_spend", fake_read)

    event = {
        "Records": [{"EventSource": "aws:sns", "Sns": {"Message": json.dumps({"spend": 999999})}}]
    }
    summary = handler_module.handler(event, _Context())

    assert seen == [ACCOUNT]
    assert summary["trigger"] == TRIGGER_ALERT
    assert summary["stopped"] == 1
    assert summary["notified"] is True
    assert summary["exit_code"] == model.EXIT_BREACH
    state = moto_ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]
    assert state["State"]["Name"] == "stopped"


def test_the_summary_log_line_carries_no_resource_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Counts only. The notification carries the detail."""
    import logging

    with caplog.at_level(logging.INFO, logger="clamper.enforcer"):
        handler_module.trigger_of(
            {"Records": [{"EventSource": "aws:sns", "Sns": {"Message": "i-0123"}}]}
        )
    assert "i-0123" not in caplog.text
