"""The terminal report: plain ASCII, no colour, no box-drawing.

It has to survive a CI log, a ticket, and an email from somebody pasting it
from their phone, so it is the lowest common denominator on purpose.
Ordered by cost within each section, so the expensive thing is the first
thing read.

Every free-text field goes through `ascii_only`: no ANSI pass-through, no
control characters, so a resource description cannot inject an escape
sequence into somebody's terminal.
"""

from __future__ import annotations

from clamper import __version__
from clamper.core.report import model
from clamper.core.report.text import ascii_only, hourly, money, percent, plural

WIDTH = 100

#: Section titles for what was done, by report kind.
ACTED_TITLES = {
    model.KIND_PLAN: "WILL CLAMP if a limit is breached",
    model.KIND_RUN: "CLAMPED",
    model.KIND_RELEASE: "RELEASED",
    model.KIND_STATUS: "CURRENTLY CLAMPED",
}


def render(report: model.Report) -> str:
    out: list[str] = []
    out.append(
        f"clamper {report.kind}  -  stack {ascii_only(report.stack_name)}, mode {report.mode}"
        + (f", regions {', '.join(report.regions)}" if report.regions else "")
    )
    out.append(f"generated {report.generated_at} by clamper {__version__}")
    # Plan and status change nothing in any mode, so the banner would only
    # suggest that something else might have.
    if report.dry_run and report.kind in {model.KIND_RUN, model.KIND_RELEASE}:
        out.append("DRY-RUN: nothing was changed.")

    if report.limits:
        out += ["", "LIMITS"]
        for limit in report.limits:
            spent = money(limit.actual_usd)
            out.append(
                f"  {limit.id:<20} {ascii_only(limit.scope):<24} {spent:>12} of "
                f"{money(limit.amount_usd):<12} {percent(limit.percent):>5}  {limit.level.upper()}"
            )
            if limit.forecast_usd is not None:
                out.append(f"      forecast {money(limit.forecast_usd)}")
            elif limit.level != "unknown":
                out.append("      forecast not available yet (AWS needs ~5 weeks of history)")
            if limit.level == "unknown":
                out.append(f"      {ascii_only(limit.reason)}")
            if limit.mismatch:
                out.append(f"      note: {ascii_only(limit.mismatch)}")

    acted_title = ACTED_TITLES.get(report.kind, "ACTED")
    if report.kind == model.KIND_RUN and report.dry_run:
        acted_title = "WOULD CLAMP (dry-run)"
    out += _section(acted_title, report.acted, total=True)
    if report.active:
        out += _section("CURRENTLY CLAMPED", report.active, total=True, clamp_ids=True)

    out += _section("NOT CLAMPED - still costing money", report.not_acted)
    if report.over_cap:
        out += [
            "",
            f"  Over the cap: {plural(len(report.not_acted), 'target')} against "
            f"max_actions_per_run {report.cap}. Clamper clamps NOTHING in this case and "
            f"waits for a person.",
        ]
    out += _section("PROTECTED - never clamped", report.protected)
    out += _section("FOUND, NOT IN v1 - Clamper will not touch these", report.unsupported)

    if report.not_selected:
        out += [
            "",
            f"NOT OPTED IN: {plural(report.not_selected, 'resource')} found without the "
            f"managed tag or a selector. Clamper will never touch them.",
        ]

    if report.errors:
        out += ["", "COULD NOT CHECK - the report has a hole here"]
        out += [f"  - {ascii_only(error)}" for error in report.errors]

    if report.release_command:
        out += ["", "To undo:", f"    {report.release_command}"]

    if report.notes:
        out += ["", "NOTES"]
        out += [f"  - {ascii_only(note)}" for note in report.notes]

    out += ["", _verdict(report)]
    return "\n".join(out) + "\n"


def _section(
    title: str,
    lines: tuple[model.Line, ...],
    *,
    total: bool = False,
    clamp_ids: bool = False,
) -> list[str]:
    if not lines:
        return []
    header = f"{title} ({len(lines)})"
    if total:
        priced = [line.hourly_usd for line in lines if line.hourly_usd is not None]
        if priced:
            unpriced = len(lines) - len(priced)
            header += f"  {hourly(sum(priced))} indicative"
            if unpriced:
                header += f", plus {unpriced} unpriced"
    out = ["", header]
    for line in lines:
        status = "" if line.status in {model.WILL_CLAMP, model.PROTECTED} else f"[{line.status}] "
        out.append(
            f"  {status}{line.title:<28} {ascii_only(line.target_id):<36} "
            f"{line.region:<14} {hourly(line.hourly_usd)}"
        )
        extra = [ascii_only(line.description)] if line.description else []
        if clamp_ids and line.clamp_id:
            extra.append(f"clamp {line.clamp_id}")
        if extra:
            out.append(f"      {'; '.join(extra)}")
        if line.detail:
            out.append(f"      {ascii_only(line.detail)}")
    return out


def _verdict(report: model.Report) -> str:
    code = report.exit_code
    if code == model.EXIT_ATTENTION:
        reasons = []
        if report.attention:
            reasons.append(f"{plural(len(report.attention), 'resource')} not done")
        if report.errors or report.unknown_limits:
            reasons.append("something could not be checked")
        return f"Exit 2: {', and '.join(reasons)}. Read NOT CLAMPED and COULD NOT CHECK above."
    if code == model.EXIT_BREACH:
        if report.over_cap:
            return "Exit 1: over the cap; nothing was clamped."
        if report.stopped_count or report.active:
            return "Exit 1: resources are clamped."
        return "Exit 1: a limit is breached."
    return "Exit 0: nothing to do."
