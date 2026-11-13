"""Turning candidates, drops and ledger entries into report lines.

Kept apart from `model.py` so the model has no dependency on discovery or
the ledger and stays a plain description of what happened.
"""

from __future__ import annotations

from clamper.core.ledger import (
    STATUS_CLAMPED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RELEASE_FAILED,
    STATUS_RELEASED,
    STATUS_SKIPPED,
    Entry,
)
from clamper.core.report import model
from clamper.core.resources import Candidate, Unsupported
from clamper.core.targets import DROP_PROTECTED, DROP_SELF, Dropped

#: Ledger status -> line status. A dry run is "would-clamp", never "clamped".
_ENTRY_STATUS = {
    STATUS_CLAMPED: model.CLAMPED,
    STATUS_DRY_RUN: model.WOULD_CLAMP,
    STATUS_FAILED: model.FAILED,
    STATUS_PENDING: model.PENDING,
    STATUS_SKIPPED: model.SKIPPED,
    STATUS_RELEASED: model.RELEASED,
    STATUS_RELEASE_FAILED: model.RELEASE_FAILED,
}


def from_candidate(candidate: Candidate, status: str, detail: str = "") -> model.Line:
    return model.Line(
        status=status,
        action_id=candidate.action_id,
        title=candidate.title,
        target_id=candidate.target_id,
        region=candidate.region,
        description=candidate.description,
        detail=detail,
        hourly_usd=candidate.hourly_usd,
        price_basis=candidate.price.basis if candidate.price else None,
    )


def from_dropped(dropped: Dropped) -> model.Line:
    status = {DROP_PROTECTED: model.PROTECTED, DROP_SELF: model.EXCLUDED}.get(
        dropped.category, model.SKIPPED
    )
    return from_candidate(dropped.candidate, status, dropped.reason)


def from_unsupported(item: Unsupported) -> model.Line:
    return model.Line(
        status=model.UNSUPPORTED,
        action_id=item.kind.lower().replace(" ", "-"),
        title=item.kind,
        target_id=item.resource_id,
        region=item.region,
        description=item.description,
        detail=item.reason,
        hourly_usd=item.hourly_usd,
        price_basis=item.price.basis if item.price else None,
    )


def from_entry(
    entry: Entry, *, candidate: Candidate | None = None, status: str | None = None
) -> model.Line:
    """A ledger entry as a line, carrying the candidate's price if known."""
    detail = entry.skip_reason or ""
    if entry.error_code or entry.error_message:
        detail = f"{entry.error_code or 'error'}: {entry.error_message or 'no detail'}"
    if entry.status == STATUS_PENDING and not detail:
        detail = "outcome unknown: the run stopped between recording and confirming"
    return model.Line(
        status=status or _ENTRY_STATUS.get(entry.status, entry.status),
        action_id=entry.target.action_id,
        title=entry.target.title,
        target_id=entry.target.target_id,
        region=entry.target.region,
        description=entry.target.description,
        detail=detail,
        hourly_usd=candidate.hourly_usd if candidate else None,
        price_basis=candidate.price.basis if candidate and candidate.price else None,
        clamp_id=entry.clamp_id,
    )
