"""JSON, versioned, documented in docs/json-schema.md.

Additive changes bump the minor version, breaking ones the major. Every key
is always present — `null` rather than absent — so a consumer never has to
ask whether a missing key and a null one mean different things.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from clamper import __version__
from clamper.core.report import model

SCHEMA_VERSION = "1.0"


def to_dict(report: model.Report) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "clamper",
        "tool_version": __version__,
        "kind": report.kind,
        "generated_at": report.generated_at,
        "stack_name": report.stack_name,
        "mode": report.mode,
        "dry_run": report.dry_run,
        "actor": report.actor or None,
        "account": report.account,
        "regions": list(report.regions),
        "exit_code": report.exit_code,
        "summary": {
            "limits_breached": len(report.breached),
            "limits_unknown": len(report.unknown_limits),
            "stopped": report.stopped_count,
            "would_stop": report.count(model.WOULD_CLAMP) + report.count(model.WILL_CLAMP),
            "released": report.count(model.RELEASED),
            "not_done": len(report.not_acted),
            "needs_attention": len(report.attention),
            "protected": len(report.protected),
            "not_selected": report.not_selected,
            "over_cap": report.over_cap,
            "cap": report.cap,
            "indicative_hourly_usd": report.total_hourly_usd,
        },
        "clamp_id": report.clamp_id,
        "release_command": report.release_command,
        "limits": [asdict(limit) for limit in report.limits],
        "acted": [asdict(line) for line in report.acted],
        "not_acted": [asdict(line) for line in report.not_acted],
        "active": [asdict(line) for line in report.active],
        "protected": [asdict(line) for line in report.protected],
        "unsupported": [asdict(line) for line in report.unsupported],
        "errors": list(report.errors),
        "notes": list(report.notes),
    }


def render(report: model.Report) -> str:
    return json.dumps(to_dict(report), indent=2) + "\n"
