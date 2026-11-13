"""`reconcile()`: read the spend, decide, record, act, report.

The same function runs in three places, which is the point of it being one
function:

- the Lambda, on an SNS alert from Budgets or on the EventBridge schedule;
- `clamper clamp`, when a person runs it by hand;
- `clamper simulate` and `clamper demo`, against an in-memory ledger with
  dry-run forced, so what somebody tries out is the code that will run.

The run, in order:

1. **Settle pending entries** left by an earlier run that died between
   record and settle, from each resource's live state. Until this happens
   they are reported as outcome unknown, never as done.
2. **Re-stop** anything Clamper stopped that AWS started again by itself —
   RDS and Aurora after seven days — and say so.
3. **Decide** every limit from `DescribeBudget`. Not from the SNS message,
   which is a doorbell and is never parsed.
4. If any limit is breached: **discover, select, and clamp** each target,
   recording before acting and settling from what the actuator reports.
   Over the cap, nothing is clamped and the report says why.

Every write goes through the one `Actuator`, built once with `dry_run` from
the config (or forced by the caller) and never changed. In dry-run the
ledger still records what *would* have happened, as `dry-run` entries, which
no counter treats as a clamp.

Importable inside a Lambda.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actions import Action, Registry, load_registry
from clamper.core.actuator import Actuator, Call, Result, clamp_call
from clamper.core.budgets import Spend
from clamper.core.config import MODE_DRY_RUN, MODE_ENFORCE, Config, Limit
from clamper.core.decide import Decision, decide
from clamper.core.discover import Discovery
from clamper.core.ledger import (
    STATUS_CLAMPED,
    STATUS_DRY_RUN,
    Clamp,
    Entry,
    LedgerStore,
    new_clamp_id,
)
from clamper.core.report import lines, model
from clamper.core.resources import Candidate, handler_for
from clamper.core.targets import DROP_PROTECTED, Selection, SelfIdentity, select

TRIGGER_ALERT = "alert"
TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"


@dataclass(frozen=True)
class Outcome:
    """What a run did, and whether anybody needs to be told."""

    report: model.Report
    #: Whether the notification should go out. Decided here rather than by
    #: the caller, so the Lambda and `clamper clamp` agree on what is worth
    #: an email.
    notify: bool


def reconcile(
    config: Config,
    *,
    actuator: Actuator,
    ledger: LedgerStore,
    identity: SelfIdentity,
    read_spend: Callable[[Limit], Spend],
    find: Callable[[tuple[Action, ...]], Discovery],
    live_state: Callable[[Entry], Mapping[str, Any] | None],
    actor: str,
    trigger: str = TRIGGER_MANUAL,
    account: str | None = None,
    registry: Registry | None = None,
    now: datetime | None = None,
) -> Outcome:
    registry = registry if registry is not None else load_registry()
    moment = now or datetime.now(UTC)
    mode = MODE_DRY_RUN if actuator.dry_run else MODE_ENFORCE
    errors: list[str] = []
    acted: list[model.Line] = []
    not_acted: list[model.Line] = []

    # 1 and 2: the ledger's own housekeeping. Never in dry-run, which acts
    # on nothing; and a dry-run ledger has no clamped entries to re-stop.
    settled = 0
    restopped = 0
    if not actuator.dry_run:
        settled_lines, settle_errors = _settle_pending(ledger, live_state, registry)
        settled = len(settled_lines)
        errors.extend(settle_errors)
        not_acted.extend(line for line in settled_lines if line.needs_attention)
        acted.extend(line for line in settled_lines if not line.needs_attention)

        restop_lines, restop_errors = _restop(ledger, actuator, live_state, registry)
        restopped = sum(1 for line in restop_lines if line.status == model.RESTOPPED)
        errors.extend(restop_errors)
        acted.extend(line for line in restop_lines if line.status == model.RESTOPPED)
        not_acted.extend(line for line in restop_lines if line.status != model.RESTOPPED)

    # 3: decide.
    decisions = [decide(read_spend(limit)) for limit in config.limits]
    breached = tuple(item.limit for item in decisions if item.breached)
    errors.extend(f"limit {item.limit.id}: {item.reason}" for item in decisions if item.unknown)

    clamp_id: str | None = None
    selection: Selection | None = None
    found: Discovery | None = None
    already_reported = False

    # 4: clamp.
    if breached:
        enabled = registry.enabled(config.targets.types)
        found = find(enabled)
        errors.extend(error.label for error in found.errors)
        selection = select(found.candidates, config, breached, identity, registry=registry)
        dropped = [
            lines.from_dropped(item)
            for item in selection.dropped
            if item.category != DROP_PROTECTED
        ]

        if selection.over_cap:
            not_acted.extend(
                lines.from_candidate(
                    item,
                    model.HELD,
                    f"{len(selection.targets)} targets is over the cap of {selection.cap}, "
                    f"so nothing was clamped. Raise safety.max_actions_per_run, or run "
                    f"`clamper clamp --yes` if this is really meant",
                )
                for item in selection.targets
            )
            not_acted.extend(dropped)
        elif selection.targets:
            already_reported = actuator.dry_run and _reported_this_period(
                ledger, breached, selection.targets, moment
            )
            if already_reported:
                not_acted.extend(dropped)
            else:
                clamp_id = new_clamp_id(now=moment)
                header = _header(clamp_id, moment, mode, actor, decisions, breached, config)
                ledger.open_clamp(header)
                done, not_done = _clamp_all(clamp_id, selection, ledger, actuator, registry)
                acted.extend(done)
                not_acted.extend(not_done)
        else:
            not_acted.extend(dropped)

    report = model.Report(
        kind=model.KIND_RUN,
        stack_name=config.stack_name,
        mode=mode,
        generated_at=moment.isoformat(timespec="seconds"),
        actor=actor,
        account=account,
        regions=found.regions if found else config.regions,
        limits=tuple(model.LimitLine.from_decision(item) for item in decisions),
        acted=model.ordered(acted),
        not_acted=model.ordered(not_acted),
        protected=model.ordered(
            [lines.from_candidate(item, model.PROTECTED) for item in selection.protected]
            if selection
            else []
        ),
        not_selected=selection.not_selected if selection else 0,
        cap=config.safety.max_actions_per_run,
        over_cap=bool(selection and selection.over_cap),
        clamp_id=clamp_id,
        errors=tuple(errors),
        notes=_notes(already_reported, actuator.dry_run),
    )
    return Outcome(
        report=report,
        notify=_worth_telling(report, trigger, clamp_id, settled, restopped, decisions),
    )


# --- 4. clamping --------------------------------------------------------


def _clamp_all(
    clamp_id: str,
    selection: Selection,
    ledger: LedgerStore,
    actuator: Actuator,
    registry: Registry,
) -> tuple[list[model.Line], list[model.Line]]:
    """Record, act, settle — for each target, in cost order.

    One target that fails never stops the others. A `LedgerError` does: a
    clamp that cannot be recorded must not happen, so it propagates and the
    run aborts with everything already settled intact.
    """
    done: list[model.Line] = []
    not_done: list[model.Line] = []

    for item in selection.dropped:
        if item.category == DROP_PROTECTED:
            continue
        ledger.skip(clamp_id, item.candidate.target, item.reason)
        not_done.append(lines.from_dropped(item))

    for candidate in selection.actionable:
        handler = handler_for(registry.get(candidate.action_id))
        entry = ledger.record(
            clamp_id, candidate.target, candidate.state, handler.applied_state(candidate)
        )
        result = actuator.invoke(
            clamp_call(
                candidate.action_id,
                candidate.target_id,
                handler.clamp_params(candidate),
                candidate.region,
            )
        )
        settled = ledger.settle(entry, result)
        line = lines.from_entry(settled, candidate=candidate)
        (done if settled.status in {STATUS_CLAMPED, STATUS_DRY_RUN} else not_done).append(line)
    return done, not_done


def _header(
    clamp_id: str,
    moment: datetime,
    mode: str,
    actor: str,
    decisions: list[Decision],
    breached: tuple[Limit, ...],
    config: Config,
) -> Clamp:
    first = next(item for item in decisions if item.breached)
    return Clamp(
        clamp_id=clamp_id,
        created_at=moment.isoformat(timespec="seconds"),
        mode=mode,
        actor=actor,
        limit_id=",".join(limit.id for limit in breached),
        limit_amount=first.limit.amount_usd,
        spend=first.spend.actual or 0.0,
        reason="; ".join(item.reason for item in decisions if item.breached),
        stack_name=config.stack_name,
    )


def _reported_this_period(
    ledger: LedgerStore,
    breached: tuple[Limit, ...],
    targets: tuple[Candidate, ...],
    moment: datetime,
) -> bool:
    """A dry run already recorded these same targets for these limits this
    month.

    Without this, a dry-run account over budget would get the same "would
    clamp" email four times a day until the month ended, and people stop
    reading an email that arrives four times a day. In enforce mode the
    question does not arise: a clamped resource is stopped, so the next run
    finds it already stopped and has nothing new to do.
    """
    period = moment.strftime("%Y-%m")
    limit_ids = ",".join(limit.id for limit in breached)
    wanted = {candidate.target.key for candidate in targets}
    for header in ledger.history(limit=20):
        if not header.dry_run or header.limit_id != limit_ids:
            continue
        if not header.created_at.startswith(period):
            continue
        recorded = {
            entry.target.key for entry in ledger.entries_for(header.clamp_id) if entry.dry_run
        }
        if wanted <= recorded:
            return True
    return False


# --- 1. settling pending entries ----------------------------------------


def _settle_pending(
    ledger: LedgerStore,
    live_state: Callable[[Entry], Mapping[str, Any] | None],
    registry: Registry,
) -> tuple[list[model.Line], list[str]]:
    """Settle each pending entry from what the resource looks like now.

    Live state equal to the applied state means the call went through before
    the run died: `clamped`. Anything else means it did not: `failed`, with a
    message saying so. Either way the entry stops being pending, and either
    way the report says it was settled after the fact.
    """
    out: list[model.Line] = []
    errors: list[str] = []
    for entry in ledger.pending():
        if entry.target.action_id not in registry.ids():
            continue
        try:
            live = live_state(entry)
        except (ClientError, BotoCoreError) as exc:
            errors.append(f"could not settle {entry.label}: {exc}")
            continue
        stopped = live is not None and dict(live) == dict(entry.applied_state)
        result = Result(
            call=_call(entry, "clamp"),
            ok=stopped,
            error_code=None if stopped else "NotApplied",
            error_message=None
            if stopped
            else (
                "an earlier run stopped between recording and confirming this clamp, and "
                "the resource is not in the clamped state now"
            ),
        )
        settled = ledger.settle(entry, result)
        line = lines.from_entry(settled)
        if stopped:
            line = replace(line, detail="settled after an interrupted run: it was stopped")
        out.append(line)
    return out, errors


# --- 2. re-stopping what AWS restarted ----------------------------------


def _restop(
    ledger: LedgerStore,
    actuator: Actuator,
    live_state: Callable[[Entry], Mapping[str, Any] | None],
    registry: Registry,
) -> tuple[list[model.Line], list[str]]:
    """Re-apply a clamp AWS undid by itself, and say so.

    Only for actions that declare `restart_after_days`. For every other
    action, a live state that differs from the applied one means a person
    changed it, which is a conflict for `release` to report — not something
    to silently undo every six hours.
    """
    out: list[model.Line] = []
    errors: list[str] = []
    for entry in ledger.active():
        try:
            action = registry.get(entry.target.action_id)
        except KeyError:
            continue
        if not action.restarts_itself:
            continue
        try:
            live = live_state(entry)
        except (ClientError, BotoCoreError) as exc:
            errors.append(f"could not check {entry.label}: {exc}")
            continue
        if live is None or dict(live) == dict(entry.applied_state):
            continue
        if dict(live) != dict(entry.previous_state):
            # Neither stopped nor back to how it was: mid-transition, most
            # likely. The next run will look again.
            continue

        handler = handler_for(action)
        candidate = handler.candidate(
            target_id=entry.target.target_id,
            region=entry.target.region,
            description=entry.target.description,
            tags={},
            state=entry.previous_state,
        )
        result = actuator.invoke(
            clamp_call(
                action.id,
                entry.target.target_id,
                handler.clamp_params(candidate),
                entry.target.region,
            )
        )
        base = lines.from_entry(entry)
        if result.performed:
            out.append(
                replace(
                    base,
                    status=model.RESTOPPED,
                    detail=(
                        f"AWS started it again by itself (it does after "
                        f"{action.restart_after_days} days stopped), so it was stopped "
                        f"again. If a person started it on purpose, use "
                        f"`clamper release {entry.clamp_id}` instead"
                    ),
                )
            )
        else:
            out.append(
                replace(
                    base,
                    status=model.FAILED,
                    detail=f"AWS restarted it and the re-stop failed: {result.summary}",
                )
            )
    return out, errors


# --- helpers ------------------------------------------------------------


def _call(entry: Entry, phase: str) -> Call:
    return Call(
        action_id=entry.target.action_id,
        phase=phase,
        params={},
        target_id=entry.target.target_id,
        region=entry.target.region,
    )


def _worth_telling(
    report: model.Report,
    trigger: str,
    clamp_id: str | None,
    settled: int,
    restopped: int,
    decisions: list[Decision],
) -> bool:
    """Whether this run's report goes out as a notification.

    Anything Clamper did, or was stopped from doing by the cap, always goes
    out. A warning goes out only when Budgets itself rang — it rings once
    per threshold per period — so a scheduled run four times a day does not
    repeat it. Errors go out on an alert or when a limit is breached, which
    is when a hole in enforcement matters.
    """
    if clamp_id or report.over_cap or settled or restopped:
        return True
    if trigger == TRIGGER_ALERT and (report.warned or report.breached or report.errors):
        return True
    return bool(report.breached and report.errors)


def _notes(already_reported: bool, dry_run: bool) -> tuple[str, ...]:
    notes: list[str] = []
    if dry_run:
        notes.append(
            "mode is dry-run: nothing was stopped. These are the resources Clamper would "
            "have stopped. Set `mode: enforce` in clamper.yml to make the limit real."
        )
    if already_reported:
        notes.append(
            "These targets were already reported as would-clamp this month, so this run "
            "recorded nothing new and sent nothing."
        )
    return tuple(notes)
