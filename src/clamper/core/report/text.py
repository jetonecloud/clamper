"""Shared text helpers for every renderer: money, safety, and words.

The safety half matters more than it looks. Resource names, descriptions and
AWS error messages are free text from somebody's account, and they reach a
terminal, a Markdown table, an email and a Slack message. Each needs its own
escaping, and they all start from the same rule: control characters never
pass through.
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean(text: object) -> str:
    """No control characters and no ANSI escapes. Newlines become spaces.

    For the terminal and the notification: an escape sequence in an instance
    description should not be able to rewrite somebody's terminal.
    """
    value = _ANSI.sub("", str(text))
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return _CONTROL.sub("", value)


def ascii_only(text: object) -> str:
    """`clean`, then anything outside printable ASCII as `?`.

    The terminal report is ASCII-only so it survives a CI log, a ticket and
    an email pasted from somebody's phone.
    """
    return "".join(char if 32 <= ord(char) < 127 else "?" for char in clean(text))


def money(amount: float | None) -> str:
    if amount is None:
        return "unknown"
    return f"${amount:,.2f}"


def hourly(amount: float | None) -> str:
    if amount is None:
        return "price unknown"
    return f"~${amount:,.3f}/hr"


def percent(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count} {word}{'' if count == 1 else suffix}"
