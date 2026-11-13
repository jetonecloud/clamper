"""`clamper status`: how close each limit is, and what is stopped right now.

Two reads: `DescribeBudget` for every limit, and the ledger's `active()` and
`pending()`. Nothing is discovered, because the question is "what has
Clamper done", and the ledger is the only place that knows.

A pending entry is listed under NOT CLAMPED as outcome unknown, which makes
the exit code 2. It is a run that died between recording and confirming,
and until the next reconcile settles it nobody knows whether that resource
is stopped.

Importable inside a Lambda, though only the CLI uses it.
"""

from __future__ import annotations

from collections.abc import Callable

from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit
from clamper.core.decide import decide
from clamper.core.ledger import LedgerError, LedgerStore
from clamper.core.report import lines, model


def build_status(
    config: Config,
    *,
    read_spend: Callable[[Limit], Spend],
    ledger: LedgerStore,
    actor: str = "",
    account: str | None = None,
) -> model.Report:
    decisions = [decide(read_spend(limit)) for limit in config.limits]
    errors: list[str] = []
    active: list[model.Line] = []
    pending: list[model.Line] = []
    try:
        active = [lines.from_entry(entry) for entry in ledger.active()]
        pending = [lines.from_entry(entry) for entry in ledger.pending()]
    except LedgerError as exc:
        # A ledger that cannot be read is not an empty one. Saying "nothing
        # is clamped" here would be the falsely-clean report.
        errors.append(f"ledger: {exc}")

    notes: list[str] = []
    if active:
        clamp_ids = sorted({line.clamp_id for line in active if line.clamp_id})
        notes.append(
            "To put everything back: `clamper release --all`. One clamp at a time: "
            + ", ".join(f"`clamper release {item}`" for item in clamp_ids)
            + "."
        )
    if config.dry_run:
        notes.append("mode is dry-run: the enforcer records and notifies but stops nothing.")

    return model.Report(
        kind=model.KIND_STATUS,
        stack_name=config.stack_name,
        mode=config.mode,
        actor=actor,
        account=account,
        regions=config.regions,
        limits=tuple(model.LimitLine.from_decision(item) for item in decisions),
        not_acted=model.ordered(pending),
        active=model.ordered(active),
        cap=config.safety.max_actions_per_run,
        errors=tuple(errors),
        notes=tuple(notes),
    )
