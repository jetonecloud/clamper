"""ok, warn, or clamp — from the spend AWS reports and nothing else.

The whole decision is a comparison, and it is kept that small on purpose.
Everything that could make it cleverer — trend lines, anomaly detection, a
"probably fine" heuristic — would also make it harder to predict, and a tool
that stops infrastructure has to be predictable above everything else. The
person who wrote `amount_usd: 500` should be able to say exactly when it
will act.

Thresholds are percentages of the limit, as in the config, so the number in
`clamper.yml` and the number in the alert AWS sends are the same number.

`notification_type: forecasted` clamps when the **forecast** crosses the
clamp threshold. It is opt-in and not the default, because AWS needs about
five weeks of history before it forecasts anything, and on a newer account
a forecast-only limit would never fire. So actual spend over the threshold
always clamps whatever the type, and a forecasted limit with no forecast yet
falls back to actual and says so.

Importable inside a Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass

from clamper.core.budgets import Spend
from clamper.core.config import NOTIFICATION_FORECASTED, Limit

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CLAMP = "clamp"
#: The spend could not be read. Never treated as ok.
LEVEL_UNKNOWN = "unknown"

LEVELS = (LEVEL_OK, LEVEL_WARN, LEVEL_CLAMP, LEVEL_UNKNOWN)


@dataclass(frozen=True)
class Decision:
    """One limit, decided."""

    limit: Limit
    spend: Spend
    level: str
    #: The figure the decision was made on, as a percentage of the limit.
    percent: float | None
    #: `actual` or `forecasted`: which figure crossed, so a report never
    #: says "you spent $900" when the $900 is a forecast.
    basis: str
    reason: str

    @property
    def breached(self) -> bool:
        return self.level == LEVEL_CLAMP

    @property
    def unknown(self) -> bool:
        return self.level == LEVEL_UNKNOWN


def decide(spend: Spend) -> Decision:
    limit = spend.limit
    if not spend.known or spend.actual is None:
        return Decision(
            limit=limit,
            spend=spend,
            level=LEVEL_UNKNOWN,
            percent=None,
            basis="actual",
            reason=spend.error or "spend unknown",
        )

    actual_percent = _percent(spend.actual, limit)
    if spend.actual >= limit.clamp_amount:
        return Decision(
            limit=limit,
            spend=spend,
            level=LEVEL_CLAMP,
            percent=actual_percent,
            basis="actual",
            reason=(
                f"actual spend ${spend.actual:,.2f} is {actual_percent:.0f}% of the "
                f"${limit.amount_usd:,.2f} limit; clamp threshold is {limit.clamp_percent:g}%"
            ),
        )

    if limit.notification_type == NOTIFICATION_FORECASTED:
        if spend.forecast is None:
            note = " (no forecast yet: AWS needs about five weeks of history)"
        elif spend.forecast >= limit.clamp_amount:
            forecast_percent = _percent(spend.forecast, limit)
            return Decision(
                limit=limit,
                spend=spend,
                level=LEVEL_CLAMP,
                percent=forecast_percent,
                basis="forecasted",
                reason=(
                    f"forecast spend ${spend.forecast:,.2f} is {forecast_percent:.0f}% of "
                    f"the ${limit.amount_usd:,.2f} limit; clamp threshold is "
                    f"{limit.clamp_percent:g}%"
                ),
            )
        else:
            note = ""
    else:
        note = ""

    if spend.actual >= limit.warn_amount:
        return Decision(
            limit=limit,
            spend=spend,
            level=LEVEL_WARN,
            percent=actual_percent,
            basis="actual",
            reason=(
                f"actual spend ${spend.actual:,.2f} is {actual_percent:.0f}% of the "
                f"${limit.amount_usd:,.2f} limit; warn threshold is "
                f"{limit.warn_percent:g}%{note}"
            ),
        )

    return Decision(
        limit=limit,
        spend=spend,
        level=LEVEL_OK,
        percent=actual_percent,
        basis="actual",
        reason=(
            f"actual spend ${spend.actual:,.2f} is {actual_percent:.0f}% of the "
            f"${limit.amount_usd:,.2f} limit{note}"
        ),
    )


def _percent(amount: float, limit: Limit) -> float:
    return amount / limit.amount_usd * 100.0
