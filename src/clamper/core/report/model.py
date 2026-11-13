"""One report object, for every command and every output format.

`plan`, a reconcile run, `status` and `release` all produce a `Report`, and
the terminal, Markdown, JSON and notification renderers are pure functions
of it. One object means one place where "how many things were clamped" is
computed, and the threat model's second headline risk — a clamp reported as
done that did not happen — has one place to be prevented.

The rule this module exists to keep: **counts come from line statuses, never
from the number of targets attempted.** A line is `clamped` only if its
ledger entry settled to `clamped`, which only happens when the actuator
reported a performed call. A dry run is `would-clamp`. An unknown outcome is
`pending`. Every one of those that is not `clamped` is listed under NOT
CLAMPED, with the reason.

Importable inside a Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from clamper.core.decide import LEVEL_CLAMP, LEVEL_UNKNOWN, LEVEL_WARN, Decision

KIND_PLAN = "plan"
KIND_RUN = "run"
KIND_STATUS = "status"
KIND_RELEASE = "release"

# --- line statuses ------------------------------------------------------
# Things that stopped, or would.
CLAMPED = "clamped"
WOULD_CLAMP = "would-clamp"
WILL_CLAMP = "will-clamp"
RESTOPPED = "re-stopped"
RELEASED = "released"
WOULD_RELEASE = "would-release"
# Still costing money, and why.
FAILED = "failed"
PENDING = "pending"
HELD = "held"
SKIPPED = "skipped"
PROTECTED = "protected"
EXCLUDED = "excluded"
UNSUPPORTED = "unsupported"
CONFLICT = "conflict"
GONE = "gone"
UNRESTORABLE = "unrestorable"
RELEASE_FAILED = "release-failed"
ERROR = "error"

#: Statuses that mean a resource **is** stopped by Clamper now.
STOPPED = frozenset({CLAMPED, RESTOPPED})

#: Statuses that are a hole in enforcement or a release nobody can trust.
#: Any of them makes the exit code 2 — the more urgent of the two non-zero
#: codes, as in Crosswind.
NEEDS_ATTENTION = frozenset({FAILED, PENDING, CONFLICT, GONE, UNRESTORABLE, RELEASE_FAILED, ERROR})

EXIT_OK = 0
EXIT_BREACH = 1
EXIT_ATTENTION = 2


@dataclass(frozen=True)
class Line:
    """One resource, and what happened to it."""

    status: str
    action_id: str
    title: str
    target_id: str
    region: str
    description: str = ""
    #: The reason for anything not done, the error for a failure, or what was
    #: restored for a release. Never a tag value.
    detail: str = ""
    hourly_usd: float | None = None
    price_basis: str | None = None
    clamp_id: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.status in NEEDS_ATTENTION


@dataclass(frozen=True)
class LimitLine:
    """One limit, as decided."""

    id: str
    amount_usd: float
    scope: str
    level: str
    actual_usd: float | None
    forecast_usd: float | None
    percent: float | None
    basis: str
    reason: str
    warn_percent: float
    clamp_percent: float
    mismatch: str | None = None

    @classmethod
    def from_decision(cls, decision: Decision) -> LimitLine:
        limit = decision.limit
        return cls(
            id=limit.id,
            amount_usd=limit.amount_usd,
            scope=limit.scope.label,
            level=decision.level,
            actual_usd=decision.spend.actual,
            forecast_usd=decision.spend.forecast,
            percent=decision.percent,
            basis=decision.basis,
            reason=decision.reason,
            warn_percent=limit.warn_percent,
            clamp_percent=limit.clamp_percent,
            mismatch=decision.spend.mismatch,
        )

    @property
    def breached(self) -> bool:
        return self.level == LEVEL_CLAMP


@dataclass(frozen=True)
class Report:
    kind: str
    stack_name: str
    mode: str
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    actor: str = ""
    account: str | None = None
    regions: tuple[str, ...] = ()
    limits: tuple[LimitLine, ...] = ()
    #: Done, or would be: clamped, would-clamp, will-clamp, re-stopped,
    #: released, would-release.
    acted: tuple[Line, ...] = ()
    #: Opted in and not done, each with its reason.
    not_acted: tuple[Line, ...] = ()
    #: Every resource carrying the protect tag, so a resource that silently
    #: stopped being protected shows up.
    protected: tuple[Line, ...] = ()
    #: Found and out of v1's reach, e.g. SageMaker endpoints.
    unsupported: tuple[Line, ...] = ()
    #: Found and never opted in. A count, not a list.
    not_selected: int = 0
    cap: int = 0
    over_cap: bool = False
    clamp_id: str | None = None
    #: Things Clamper could not check at all. Each is a hole in the report.
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: For `status`: what is currently stopped by Clamper.
    active: tuple[Line, ...] = ()

    @property
    def dry_run(self) -> bool:
        return self.mode != "enforce"

    @property
    def breached(self) -> tuple[LimitLine, ...]:
        return tuple(limit for limit in self.limits if limit.level == LEVEL_CLAMP)

    @property
    def warned(self) -> tuple[LimitLine, ...]:
        return tuple(limit for limit in self.limits if limit.level == LEVEL_WARN)

    @property
    def unknown_limits(self) -> tuple[LimitLine, ...]:
        return tuple(limit for limit in self.limits if limit.level == LEVEL_UNKNOWN)

    def count(self, status: str) -> int:
        """How many lines have this status. The only counter there is."""
        return sum(1 for line in (*self.acted, *self.not_acted) if line.status == status)

    @property
    def stopped_count(self) -> int:
        return sum(1 for line in self.acted if line.status in STOPPED)

    @property
    def attention(self) -> tuple[Line, ...]:
        return tuple(line for line in self.not_acted if line.needs_attention)

    @property
    def release_command(self) -> str | None:
        if self.clamp_id and self.stopped_count:
            return f"clamper release {self.clamp_id}"
        return None

    @property
    def exit_code(self) -> int:
        """0 nothing to do, 1 breached or clamped, 2 something unchecked or
        not done. 2 beats 1."""
        if self.errors or self.attention or self.unknown_limits:
            return EXIT_ATTENTION
        if self.breached or self.over_cap or self.stopped_count or self.active:
            return EXIT_BREACH
        return EXIT_OK

    @property
    def total_hourly_usd(self) -> float | None:
        """The indicative hourly figure of what was (or would be) stopped.

        `None` if nothing stopped carries a figure. Partial sums are labelled
        as such by the renderers, which count the unpriced lines alongside.
        """
        priced = [line.hourly_usd for line in self.acted if line.hourly_usd is not None]
        return round(sum(priced), 6) if priced else None


def ordered(lines: list[Line] | tuple[Line, ...]) -> tuple[Line, ...]:
    """Most expensive first, unpriced after, then stable by type and id."""
    return tuple(
        sorted(
            lines,
            key=lambda line: (
                0 if line.hourly_usd is not None else 1,
                -(line.hourly_usd or 0.0),
                line.action_id,
                line.target_id,
            ),
        )
    )
