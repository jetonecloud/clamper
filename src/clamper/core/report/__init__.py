"""Renderers. Each is a pure function of a `model.Report`.

No HTML. Crosswind has one because a drift report goes to a manager who did
not run the tool; Clamper's output goes to whoever is on call, and the format
that matters is the one that survives being pasted into Slack.
"""

from __future__ import annotations

from collections.abc import Callable

from clamper.core.report import json_out, markdown, model, terminal

FORMATS = ("terminal", "markdown", "json")
EXTENSIONS = {"terminal": "txt", "markdown": "md", "json": "json"}

_RENDERERS: dict[str, Callable[[model.Report], str]] = {
    "terminal": terminal.render,
    "markdown": markdown.render,
    "json": json_out.render,
}


def render(report: model.Report, fmt: str) -> str:
    try:
        return _RENDERERS[fmt](report)
    except KeyError:
        raise ValueError(f"{fmt!r} is not a format. One of: {', '.join(FORMATS)}.") from None
