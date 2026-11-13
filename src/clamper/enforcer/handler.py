"""The Lambda entry point.

Three ways in, one code path. The event is used for exactly one thing — to
tell which of the three it was — and is otherwise ignored:

- An SNS record from the alerts topic: Budgets rang. **The message body is
  never parsed.** Its length is logged and nothing else is read from it. The
  decision comes from `DescribeBudget`, so a spoofed "you have breached
  $5000" causes a reconcile that reads the real spend and does nothing.
- `{"source": "clamper.schedule"}` from EventBridge: the reconcile schedule.
- Anything else: an invocation by hand, from the console or the CLI.

The config is the `clamper.yml` that `clamper deploy` packed into the zip,
next to the `clamper` package. A config change is therefore a new zip and a
stack update, which needs rights to update the stack — the threat model
relies on that.

Logging: counts, mode, trigger and exit code. No account id, resource id or
ARN at any level above INFO, and the notification carries the detail.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import clamper
from clamper.core import config as config_module
from clamper.core.actions import load_registry
from clamper.core.actuator import Actuator
from clamper.core.budgets import read_spend
from clamper.core.discover import discover
from clamper.core.ledger import Ledger, table_name_from_env
from clamper.core.resources import handler_for
from clamper.core.session import Clients, build_session
from clamper.core.targets import SelfIdentity
from clamper.enforcer import notify
from clamper.enforcer.run import TRIGGER_ALERT, TRIGGER_MANUAL, TRIGGER_SCHEDULE, reconcile

log = logging.getLogger("clamper.enforcer")
log.setLevel(logging.INFO)

CONFIG_ENV = "CLAMPER_CONFIG_PATH"
TOPIC_ENV = "CLAMPER_TOPIC_ARN"


def config_path() -> Path:
    """The packed `clamper.yml`: beside the package at the zip's root."""
    override = os.environ.get(CONFIG_ENV, "").strip()
    if override:
        return Path(override)
    return Path(clamper.__file__).resolve().parent.parent / config_module.CONFIG_FILENAME


def trigger_of(event: Any) -> str:
    if isinstance(event, Mapping):
        records = event.get("Records")
        if isinstance(records, list) and records:
            first = records[0] if isinstance(records[0], Mapping) else {}
            if first.get("EventSource") == "aws:sns" or "Sns" in first:
                message = first.get("Sns", {}).get("Message", "")
                log.info("woken by SNS; message of %d characters not parsed", len(str(message)))
                return TRIGGER_ALERT
        if event.get("source") == "clamper.schedule":
            return TRIGGER_SCHEDULE
    return TRIGGER_MANUAL


def account_of(context: Any) -> str:
    """The account id, from the function's own ARN.

    No `sts:GetCallerIdentity`: the role deliberately has no sts permission,
    and the invoked function's ARN already says which account this is.
    """
    arn = str(getattr(context, "invoked_function_arn", "") or "")
    parts = arn.split(":")
    if len(parts) < 5 or not parts[4].isdigit():
        raise RuntimeError("could not read the account id from the invoked function ARN")
    return parts[4]


def handler(event: Any, context: Any) -> dict[str, Any]:
    trigger = trigger_of(event)
    config = config_module.load(config_path())
    clients = Clients(build_session())
    account = account_of(context)
    registry = load_registry()
    regions = config.regions or ((clients.default_region,) if clients.default_region else ())

    outcome = reconcile(
        config,
        actuator=Actuator(clients, dry_run=config.dry_run, registry=registry),
        ledger=Ledger(clients, table_name_from_env()),
        identity=SelfIdentity.from_env(),
        read_spend=lambda limit: read_spend(clients, account, limit, config.stack_name),
        find=lambda actions: discover(clients, actions, regions),
        live_state=lambda entry: handler_for(registry.get(entry.target.action_id)).live_state(
            clients, entry
        ),
        actor=f"enforcer:{trigger}",
        trigger=trigger,
        account=account,
        registry=registry,
    )
    report = outcome.report

    delivered: notify.Delivery | None = None
    if outcome.notify:
        delivered = notify.send(
            report,
            clients=clients,
            topic_arn=os.environ.get(TOPIC_ENV, ""),
            slack_parameter=config.notifications.slack_webhook_parameter,
        )

    summary = {
        "trigger": trigger,
        "mode": report.mode,
        "exit_code": report.exit_code,
        "limits_breached": len(report.breached),
        "stopped": report.stopped_count,
        "would_stop": report.count("would-clamp"),
        "not_stopped": len(report.not_acted),
        "needs_attention": len(report.attention),
        "errors": len(report.errors),
        "over_cap": report.over_cap,
        "notified": bool(delivered and delivered.sns),
    }
    log.info("reconcile finished: %s", summary)
    return summary
