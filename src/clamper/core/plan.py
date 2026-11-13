"""`clamper plan`: what would be clamped if every limit were breached now.

Read-only by construction. It discovers, selects and reports, and it never
builds an `Actuator` — so there is no object in scope that could make a
write. That is a stronger property than a dry-run flag, and it is why the
shipped CI example runs `clamper plan` and nothing else.

Every limit is treated as breached, because the question a plan answers is
"what have I told Clamper it may stop?", and the answer should not depend on
whether this month happens to be over budget. The current spend is shown
alongside so the reader also knows how close that is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from clamper.core.actions import load_registry
from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit
from clamper.core.decide import decide
from clamper.core.discover import Discovery
from clamper.core.report import lines, model
from clamper.core.targets import DROP_PROTECTED, SelfIdentity, select


def build_plan(
    config: Config,
    *,
    read_spend: Callable[[Limit], Spend],
    find: Callable[[], Discovery],
    identity: SelfIdentity,
    actor: str = "",
    account: str | None = None,
) -> model.Report:
    registry = load_registry()
    decisions = [decide(read_spend(limit)) for limit in config.limits]
    found = find()
    selection = select(found.candidates, config, config.limits, identity, registry=registry)

    # Over the cap the targets move to NOT CLAMPED as "held": the plan has
    # to show that nothing would happen, not a list that looks actionable.
    targets = [
        lines.from_candidate(
            item,
            model.HELD if selection.over_cap else model.WILL_CLAMP,
            f"over the cap of {selection.cap}: Clamper would clamp nothing and notify"
            if selection.over_cap
            else "",
        )
        for item in selection.targets
    ]
    # Protected resources have their own section, so they are not repeated.
    dropped = [
        lines.from_dropped(item) for item in selection.dropped if item.category != DROP_PROTECTED
    ]
    acted, not_acted = ([], targets + dropped) if selection.over_cap else (targets, dropped)

    notes = _notes(config, registry.enabled(config.targets.types))
    errors = tuple(error.label for error in found.errors)
    return model.Report(
        kind=model.KIND_PLAN,
        stack_name=config.stack_name,
        mode=config.mode,
        actor=actor,
        account=account,
        regions=found.regions,
        limits=tuple(model.LimitLine.from_decision(item) for item in decisions),
        acted=model.ordered(acted),
        not_acted=model.ordered(not_acted),
        protected=model.ordered(
            [lines.from_candidate(item, model.PROTECTED) for item in selection.protected]
        ),
        unsupported=model.ordered([lines.from_unsupported(item) for item in found.unsupported]),
        not_selected=selection.not_selected,
        cap=selection.cap,
        over_cap=selection.over_cap,
        errors=errors,
        notes=notes,
    )


def _notes(config: Config, actions: Iterable[object]) -> tuple[str, ...]:
    notes = [
        "Figures are indicative us-east-1 on-demand list prices for ranking, not a bill. "
        "Storage keeps billing while compute is stopped.",
        "A plan treats every limit as breached. The limits section shows how close each "
        "one actually is.",
    ]
    if config.dry_run:
        notes.append(
            "mode is dry-run: the enforcer records and notifies but does not act. "
            "Set `mode: enforce` in clamper.yml when you are ready."
        )
    if config.targets.include_untagged:
        notes.append(
            "include_untagged is on: resources nobody tagged are candidates. "
            "Only clamper:protect keeps one out."
        )
    return tuple(notes)
