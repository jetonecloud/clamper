"""The ledger, as `clamper ledger` prints it.

Two views: one line per clamp run, and every entry of one run. Plain ASCII
like the terminal report, and JSON for anything that wants to parse it.
Free text — descriptions, error messages, skip reasons — goes through
`ascii_only`, because the ledger is untrusted input as far as a terminal is
concerned.

Importable inside a Lambda, though only the CLI uses it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from clamper.core.ledger import Clamp, Entry
from clamper.core.report.text import ascii_only, money

HISTORY_SCHEMA_VERSION = "1.0"


def history_text(clamps: Sequence[Clamp], table: str) -> str:
    if not clamps:
        return f"The ledger ({ascii_only(table)}) has no clamp runs yet.\n"
    out = [f"Clamp runs in {ascii_only(table)}, newest first:", ""]
    out.append(f"  {'clamp id':<24} {'when':<26} {'mode':<8} {'limit':<20} {'spend':>12}  actor")
    for clamp in clamps:
        out.append(
            f"  {clamp.clamp_id:<24} {clamp.created_at:<26} {clamp.mode:<8} "
            f"{ascii_only(clamp.limit_id):<20} {money(clamp.spend):>12}  {ascii_only(clamp.actor)}"
        )
    out += ["", "Show one: clamper ledger <clamp-id>"]
    return "\n".join(out) + "\n"


def clamp_text(clamp: Clamp) -> str:
    out = [
        f"clamp {clamp.clamp_id}",
        f"  when:   {clamp.created_at}",
        f"  mode:   {clamp.mode}" + ("  (nothing was stopped)" if clamp.dry_run else ""),
        f"  actor:  {ascii_only(clamp.actor)}",
        f"  limit:  {ascii_only(clamp.limit_id)} {money(clamp.limit_amount)}, "
        f"spend {money(clamp.spend)}",
        f"  why:    {ascii_only(clamp.reason)}",
        "",
        f"{len(clamp.entries)} {'entry' if len(clamp.entries) == 1 else 'entries'}:",
    ]
    for entry in clamp.entries:
        out += _entry(entry)
    if clamp.active:
        out += ["", "To undo:", f"    {clamp.release_command}"]
    return "\n".join(out) + "\n"


def _entry(entry: Entry) -> list[str]:
    lines = [
        f"  [{entry.status}] {entry.target.title:<28} {ascii_only(entry.target.target_id):<36} "
        f"{entry.target.region}"
    ]
    if entry.previous_state:
        lines.append(f"      before:  {_state(entry.previous_state)}")
    if entry.applied_state:
        lines.append(f"      clamped: {_state(entry.applied_state)}")
    if entry.error_code or entry.error_message:
        lines.append(
            f"      error:   {ascii_only(entry.error_code or 'error')}: "
            f"{ascii_only(entry.error_message or 'no detail')}"
        )
    if entry.skip_reason:
        lines.append(f"      skipped: {ascii_only(entry.skip_reason)}")
    lines.append(f"      updated: {entry.updated_at}")
    return lines


def _state(state: Any) -> str:
    return ", ".join(
        f"{key}={'none' if value is None else ascii_only(value)}"
        for key, value in sorted(state.items())
    )


def history_json(clamps: Sequence[Clamp]) -> str:
    return _dump({"schema_version": HISTORY_SCHEMA_VERSION, "clamps": [_header(c) for c in clamps]})


def clamp_json(clamp: Clamp) -> str:
    document = _header(clamp)
    document["entries"] = [_entry_dict(entry) for entry in clamp.entries]
    return _dump({"schema_version": HISTORY_SCHEMA_VERSION, "clamp": document})


def _header(clamp: Clamp) -> dict[str, Any]:
    return {
        "clamp_id": clamp.clamp_id,
        "created_at": clamp.created_at,
        "mode": clamp.mode,
        "actor": clamp.actor,
        "limit_id": clamp.limit_id,
        "limit_amount_usd": clamp.limit_amount,
        "spend_usd": clamp.spend,
        "reason": clamp.reason,
        "stack_name": clamp.stack_name,
    }


def _entry_dict(entry: Entry) -> dict[str, Any]:
    return {
        "status": entry.status,
        "action_id": entry.target.action_id,
        "title": entry.target.title,
        "target_id": entry.target.target_id,
        "region": entry.target.region,
        "description": entry.target.description,
        "previous_state": dict(entry.previous_state),
        "applied_state": dict(entry.applied_state),
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "dry_run": entry.dry_run,
        "error_code": entry.error_code,
        "error_message": entry.error_message,
        "skip_reason": entry.skip_reason,
    }


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False, default=str) + "\n"
