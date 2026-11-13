"""reconcile(): the whole run, end to end, against moto.

The actuator, ledger, handlers, discovery and selection are the real ones.
Only AWS is replaced, and the spend, which is passed in so a test can say
"over budget" without a Budgets stub per case.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from support import REGION
from test_resource_ec2 import launch
from test_resource_rds import make_instance

from clamper.core.actions import load_registry
from clamper.core.actuator import Actuator
from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit, loads
from clamper.core.discover import discover
from clamper.core.ledger import STATUS_CLAMPED, STATUS_DRY_RUN, STATUS_SKIPPED, MemoryLedger
from clamper.core.report import model
from clamper.core.resources import handler_for
from clamper.core.targets import SelfIdentity
from clamper.enforcer.run import TRIGGER_ALERT, TRIGGER_SCHEDULE, Outcome, reconcile

MANAGED = {"clamper:managed": "true"}


def config(mode: str = "enforce", extra: str = "") -> Config:
    return loads(
        f"version: 1\nmode: {mode}\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n" + extra
    )


def spend_of(amount: float | None) -> Callable[[Limit], Spend]:
    def read(limit: Limit) -> Spend:
        return Spend(
            limit=limit,
            budget_name=limit.budget_name("clamper"),
            actual=amount,
            error=None if amount is not None else "AccessDeniedException - nope",
        )

    return read


def run(
    clients: Any,
    cfg: Config,
    *,
    spend: float | None,
    ledger: MemoryLedger | None = None,
    trigger: str = TRIGGER_SCHEDULE,
) -> tuple[Outcome, MemoryLedger]:
    chosen = ledger or MemoryLedger()
    registry = load_registry()
    outcome = reconcile(
        cfg,
        actuator=Actuator(clients, dry_run=cfg.dry_run),
        ledger=chosen,
        identity=SelfIdentity("clamper"),
        read_spend=spend_of(spend),
        find=lambda actions: discover(clients, actions, cfg.regions),
        live_state=lambda entry: handler_for(registry.get(entry.target.action_id)).live_state(
            clients, entry
        ),
        actor="test",
        trigger=trigger,
    )
    return outcome, chosen


def state_of(ec2: Any, instance_id: str) -> str:
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return str(reservations[0]["Instances"][0]["State"]["Name"])


# --- under the limit ----------------------------------------------------


def test_under_the_limit_nothing_happens(clients_moto: Any, moto_ec2: Any) -> None:
    instance = launch(moto_ec2, tags=MANAGED)
    outcome, ledger = run(clients_moto, config(), spend=10.0)

    assert outcome.report.acted == ()
    assert outcome.report.exit_code == model.EXIT_OK
    assert outcome.notify is False
    assert ledger.history() == ()
    assert state_of(moto_ec2, instance) == "running"


def test_a_warning_is_told_only_when_budgets_rang(clients_moto: Any) -> None:
    """Budgets rings once per threshold per period. A schedule four times a
    day repeating the warning would be noise."""
    scheduled, _ = run(clients_moto, config(), spend=85.0, trigger=TRIGGER_SCHEDULE)
    alerted, _ = run(clients_moto, config(), spend=85.0, trigger=TRIGGER_ALERT)
    assert scheduled.notify is False
    assert alerted.notify is True


# --- a breach -----------------------------------------------------------


def test_a_breach_in_enforce_mode_stops_the_managed_resource(
    clients_moto: Any, moto_ec2: Any
) -> None:
    managed = launch(moto_ec2, tags=MANAGED)
    untagged = launch(moto_ec2)

    outcome, ledger = run(clients_moto, config(), spend=150.0)
    report = outcome.report

    assert state_of(moto_ec2, managed) == "stopped"
    assert state_of(moto_ec2, untagged) == "running"
    assert [line.target_id for line in report.acted] == [managed]
    assert report.acted[0].status == model.CLAMPED
    assert report.stopped_count == 1
    assert report.not_selected == 1
    assert report.clamp_id is not None
    assert report.release_command == f"clamper release {report.clamp_id}"
    assert report.exit_code == model.EXIT_BREACH
    assert outcome.notify is True
    assert [entry.status for entry in ledger.active()] == [STATUS_CLAMPED]


def test_the_next_run_has_nothing_new_to_do(clients_moto: Any, moto_ec2: Any) -> None:
    """In enforce mode a clamped resource is stopped, so the next reconcile
    finds it already stopped and opens no new run and sends nothing."""
    launch(moto_ec2, tags=MANAGED)
    first, ledger = run(clients_moto, config(), spend=150.0)
    second, _ = run(clients_moto, config(), spend=150.0, ledger=ledger)

    assert second.report.clamp_id is None
    assert second.report.acted == ()
    assert second.notify is False
    assert len(ledger.history()) == 1
    assert "already stopped" in second.report.not_acted[0].detail


def test_dry_run_records_but_does_not_act(clients_moto: Any, moto_ec2: Any) -> None:
    instance = launch(moto_ec2, tags=MANAGED)
    outcome, ledger = run(clients_moto, config("dry-run"), spend=150.0)

    assert state_of(moto_ec2, instance) == "running"
    assert outcome.report.acted[0].status == model.WOULD_CLAMP
    assert outcome.report.stopped_count == 0
    assert outcome.notify is True
    entries = ledger.entries_for(outcome.report.clamp_id or "")
    assert [entry.status for entry in entries] == [STATUS_DRY_RUN]
    assert ledger.active() == ()


def test_dry_run_does_not_repeat_itself_within_the_month(clients_moto: Any, moto_ec2: Any) -> None:
    launch(moto_ec2, tags=MANAGED)
    _, ledger = run(clients_moto, config("dry-run"), spend=150.0)
    again, _ = run(clients_moto, config("dry-run"), spend=150.0, ledger=ledger)

    assert again.notify is False
    assert again.report.clamp_id is None
    assert len(ledger.history()) == 1
    assert any("already reported" in note for note in again.report.notes)


def test_protected_and_own_resources_are_not_touched(clients_moto: Any, moto_ec2: Any) -> None:
    protected = launch(moto_ec2, tags={**MANAGED, "clamper:protect": "true"})
    own = launch(moto_ec2, tags={**MANAGED, "aws:cloudformation:stack-name": "clamper"})

    outcome, ledger = run(clients_moto, config(), spend=150.0)

    assert state_of(moto_ec2, protected) == "running"
    assert state_of(moto_ec2, own) == "running"
    assert outcome.report.clamp_id is None
    assert [line.target_id for line in outcome.report.protected] == [protected]
    assert [(line.target_id, line.status) for line in outcome.report.not_acted] == [
        (own, model.EXCLUDED)
    ]


def test_skips_are_written_to_the_ledger_when_a_run_opens(clients_moto: Any, moto_ec2: Any) -> None:
    """So "nothing was clamped" and "this was skipped for a reason" are
    distinguishable a month later."""
    launch(moto_ec2, tags=MANAGED)
    launch(moto_ec2, tags={**MANAGED, "aws:autoscaling:groupName": "web"})
    outcome, ledger = run(clients_moto, config(), spend=150.0)
    statuses = sorted(entry.status for entry in ledger.entries_for(outcome.report.clamp_id or ""))
    assert statuses == [STATUS_CLAMPED, STATUS_SKIPPED]


# --- the cap ------------------------------------------------------------


def test_over_the_cap_nothing_is_stopped_and_somebody_is_told(
    clients_moto: Any, moto_ec2: Any
) -> None:
    instances = [launch(moto_ec2, tags=MANAGED) for _ in range(3)]
    outcome, ledger = run(
        clients_moto, config(extra="safety:\n  max_actions_per_run: 2\n"), spend=150.0
    )

    assert all(state_of(moto_ec2, item) == "running" for item in instances)
    assert outcome.report.over_cap is True
    assert outcome.report.acted == ()
    assert {line.status for line in outcome.report.not_acted} == {model.HELD}
    assert outcome.notify is True
    assert ledger.history() == ()
    assert outcome.report.exit_code == model.EXIT_BREACH


# --- honesty about failure ----------------------------------------------


def test_unreadable_spend_is_a_hole_not_a_pass(clients_moto: Any) -> None:
    outcome, _ = run(clients_moto, config(), spend=None, trigger=TRIGGER_ALERT)
    assert outcome.report.exit_code == model.EXIT_ATTENTION
    assert any("AccessDenied" in error for error in outcome.report.errors)
    assert outcome.notify is True


def test_a_failed_clamp_is_never_counted_as_done(clients_moto: Any, moto_ec2: Any) -> None:
    """Discovery finds an instance; it is terminated before the call. The
    stop fails, and every count says so."""
    cfg = config()
    instance = launch(moto_ec2, tags=MANAGED)
    found = discover(clients_moto, load_registry().enabled(cfg.targets.types), cfg.regions)
    moto_ec2.terminate_instances(InstanceIds=[instance])
    ghost = [
        handler_for(load_registry().get("ec2-instance")).candidate(
            target_id="i-00000000000000000",
            region=REGION,
            description="m5.large, running",
            tags=MANAGED,
            state={"state": "running"},
        )
    ]
    registry = load_registry()
    ledger = MemoryLedger()
    outcome = reconcile(
        cfg,
        actuator=Actuator(clients_moto, dry_run=False),
        ledger=ledger,
        identity=SelfIdentity("clamper"),
        read_spend=spend_of(150.0),
        find=lambda actions: type(found)(candidates=tuple(ghost), regions=cfg.regions),
        live_state=lambda entry: handler_for(registry.get(entry.target.action_id)).live_state(
            clients_moto, entry
        ),
        actor="test",
    )
    report = outcome.report
    assert report.stopped_count == 0
    assert report.acted == ()
    assert report.not_acted[0].status == model.FAILED
    assert "InvalidInstanceID" in report.not_acted[0].detail
    assert report.exit_code == model.EXIT_ATTENTION
    assert ledger.active() == ()


# --- housekeeping -------------------------------------------------------


def test_a_pending_entry_is_settled_from_live_state(clients_moto: Any, moto_ec2: Any) -> None:
    """A run died after recording and after the call. The next run sees the
    instance stopped and settles the entry as clamped."""
    instance = launch(moto_ec2, tags=MANAGED)
    ledger = MemoryLedger()
    candidate = next(
        item
        for item in handler_for(load_registry().get("ec2-instance")).discover(clients_moto, REGION)
        if item.target_id == instance
    )
    ledger.record(
        "20261113t153000z-a1b2c3", candidate.target, {"state": "running"}, {"state": "stopped"}
    )
    moto_ec2.stop_instances(InstanceIds=[instance])

    outcome, _ = run(clients_moto, config(), spend=10.0, ledger=ledger)

    assert ledger.pending() == ()
    assert [entry.status for entry in ledger.active()] == [STATUS_CLAMPED]
    assert "settled after an interrupted run" in outcome.report.acted[0].detail
    assert outcome.notify is True


def test_a_pending_entry_whose_call_never_happened_settles_as_failed(
    clients_moto: Any, moto_ec2: Any
) -> None:
    instance = launch(moto_ec2, tags=MANAGED)
    ledger = MemoryLedger()
    candidate = next(
        item
        for item in handler_for(load_registry().get("ec2-instance")).discover(clients_moto, REGION)
        if item.target_id == instance
    )
    ledger.record(
        "20261113t153000z-a1b2c3", candidate.target, {"state": "running"}, {"state": "stopped"}
    )

    outcome, _ = run(clients_moto, config(), spend=10.0, ledger=ledger)

    assert ledger.pending() == ()
    assert ledger.active() == ()
    assert outcome.report.not_acted[0].status == model.FAILED
    assert outcome.report.exit_code == model.EXIT_ATTENTION


def test_rds_restarted_by_aws_is_stopped_again_and_reported(
    clients_moto: Any, moto_rds: Any
) -> None:
    make_instance(moto_rds, tags=MANAGED)
    _, ledger = run(clients_moto, config(), spend=150.0)
    # Seven days later, AWS starts it again by itself.
    moto_rds.start_db_instance(DBInstanceIdentifier="orders-db")

    outcome, _ = run(clients_moto, config(), spend=10.0, ledger=ledger)

    status = moto_rds.describe_db_instances(DBInstanceIdentifier="orders-db")["DBInstances"][0]
    assert status["DBInstanceStatus"] == "stopped"
    (line,) = outcome.report.acted
    assert line.status == model.RESTOPPED
    assert "by itself" in line.detail
    assert outcome.notify is True


def test_an_ec2_instance_started_by_hand_is_not_silently_re_stopped(
    clients_moto: Any, moto_ec2: Any
) -> None:
    """Only actions AWS undoes on its own are re-stopped. Anything else that
    changed was changed by a person, and that is release's conflict to
    report, not something to undo every six hours."""
    instance = launch(moto_ec2, tags=MANAGED)
    _, ledger = run(clients_moto, config(), spend=150.0)
    moto_ec2.start_instances(InstanceIds=[instance])

    outcome, _ = run(clients_moto, config(), spend=10.0, ledger=ledger)
    assert state_of(moto_ec2, instance) == "running"
    assert outcome.report.acted == ()


@pytest.mark.parametrize("mode", ["dry-run"])
def test_dry_run_never_does_housekeeping_writes(
    clients_moto: Any, moto_ec2: Any, mode: str
) -> None:
    instance = launch(moto_ec2, tags=MANAGED)
    ledger = MemoryLedger()
    candidate = next(
        item
        for item in handler_for(load_registry().get("ec2-instance")).discover(clients_moto, REGION)
        if item.target_id == instance
    )
    ledger.record(
        "20261113t153000z-a1b2c3", candidate.target, {"state": "running"}, {"state": "stopped"}
    )
    run(clients_moto, config(mode), spend=10.0, ledger=ledger)
    assert len(ledger.pending()) == 1
