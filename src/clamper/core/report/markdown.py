"""Markdown, for a PR comment or a GitHub job summary.

Free text is escaped for a table cell: pipes would break the table, and
backticks and angle brackets would let a resource name start a code span or
an HTML tag in somebody's rendered PR.
"""

from __future__ import annotations

from clamper import __version__
from clamper.core.report import model
from clamper.core.report.terminal import ACTED_TITLES
from clamper.core.report.text import clean, hourly, money, percent, plural

_ESCAPES = str.maketrans(
    {
        "|": "\\|",
        "`": "\\`",
        "<": "&lt;",
        ">": "&gt;",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
    }
)


def cell(text: object) -> str:
    return clean(text).translate(_ESCAPES)


def render(report: model.Report) -> str:
    out: list[str] = [
        f"## clamper {report.kind}: `{cell(report.stack_name)}` ({report.mode})",
        "",
        f"Generated {report.generated_at} by clamper {__version__}"
        + (f" across {', '.join(report.regions)}" if report.regions else "")
        + ".",
    ]
    if report.dry_run and report.kind != model.KIND_PLAN:
        out += ["", "> **Dry-run.** Nothing was changed."]

    if report.limits:
        out += ["", "| Limit | Scope | Spend | Limit | % | Level |", "|---|---|---:|---:|---:|---|"]
        for limit in report.limits:
            out.append(
                f"| {cell(limit.id)} | {cell(limit.scope)} | {money(limit.actual_usd)} | "
                f"{money(limit.amount_usd)} | {percent(limit.percent)} | **{limit.level}** |"
            )

    title = ACTED_TITLES.get(report.kind, "Acted")
    if report.kind == model.KIND_RUN and report.dry_run:
        title = "Would clamp (dry-run)"
    out += _table(title, report.acted)
    out += _table("Currently clamped", report.active)
    out += _table("Not clamped (still costing money)", report.not_acted)
    if report.over_cap:
        out += [
            "",
            f"> **Over the cap** of {report.cap}. Clamper clamps nothing in this case and "
            f"waits for a person.",
        ]
    out += _table("Protected", report.protected)
    out += _table("Found, not in v1", report.unsupported)
    if report.not_selected:
        out += ["", f"{plural(report.not_selected, 'resource')} found and never opted in."]
    if report.errors:
        out += ["", "### Could not check", ""]
        out += [f"- {cell(error)}" for error in report.errors]
    if report.release_command:
        out += ["", "To undo:", "", "```sh", report.release_command, "```"]
    if report.notes:
        out += [""] + [f"- {cell(note)}" for note in report.notes]
    out += ["", f"Exit code **{report.exit_code}**."]
    return "\n".join(out) + "\n"


def _table(title: str, lines: tuple[model.Line, ...]) -> list[str]:
    if not lines:
        return []
    out = [
        "",
        f"### {title} ({len(lines)})",
        "",
        "| Status | Resource | Id | Region | Hourly | Detail |",
        "|---|---|---|---|---:|---|",
    ]
    for line in lines:
        detail = "; ".join(item for item in (line.description, line.detail) if item)
        out.append(
            f"| {line.status} | {cell(line.title)} | `{clean(line.target_id).replace('`', '')}` | "
            f"{cell(line.region)} | {hourly(line.hourly_usd)} | {cell(detail)} |"
        )
    return out
