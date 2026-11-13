"""What AWS Budgets says the spend is, now.

Clamper never decides from the SNS message that woke it. The notification is
a doorbell: the payload format of a budget alert is not a documented,
versioned contract, a replayed message is a month old, and a spoofed one says
whatever its author wanted. So every decision rests on `DescribeBudget`,
called at the moment of deciding.

`DescribeBudget` is not charged. `ce:GetCostAndUsage` is $0.01 a request,
and four reconciles a day across three limits would be $3.60 a month to
learn what Budgets says for free — docs/design.md has the arithmetic. Cost
Explorer is not used here at all.

Two facts from AWS's documentation shape what this module returns:

- Budgets "is updated up to three times a day", so a figure here can be
  hours old. `Spend` carries no pretence otherwise.
- Forecasts need "approximately 5 weeks of usage data". A missing
  `ForecastedSpend` is `None`, never zero, and reports say "not available
  yet" — a zero forecast and an absent one mean opposite things.

Importable inside a Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.config import Limit
from clamper.core.session import Clients


@dataclass(frozen=True)
class Spend:
    """One limit's budget, as AWS reports it.

    `error` set means the spend is **unknown**, which is never the same as
    zero: a limit Clamper could not read is a limit it could not enforce,
    and that makes the exit code 2.
    """

    limit: Limit
    budget_name: str
    actual: float | None = None
    forecast: float | None = None
    #: The amount AWS holds for the budget. Compared with the config, because
    #: a budget edited by hand in the console and a config that says
    #: something else are two different limits and somebody should know.
    budget_amount: float | None = None
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.error is None and self.actual is not None

    @property
    def mismatch(self) -> str | None:
        """A sentence if the deployed budget disagrees with the config."""
        if self.budget_amount is None:
            return None
        if abs(self.budget_amount - self.limit.amount_usd) < 0.005:
            return None
        return (
            f"the deployed budget is ${self.budget_amount:,.2f} but clamper.yml says "
            f"${self.limit.amount_usd:,.2f}. Clamper decides from clamper.yml; run "
            f"`clamper deploy` to bring the budget back in line"
        )


def read_spend(clients: Clients, account: str, limit: Limit, stack_name: str) -> Spend:
    """Ask Budgets for one limit's actual and forecast spend.

    Never raises for an AWS-side problem. A limit that could not be read is a
    `Spend` with `error` set, so one unreadable budget does not stop the
    others being enforced.
    """
    name = limit.budget_name(stack_name)
    client = clients.get("budgets")
    try:
        response = client.describe_budget(AccountId=account, BudgetName=name)
    except ClientError as exc:
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = str(error.get("Code") or "ClientError")
        if code == "NotFoundException":
            message = (
                f"budget {name} does not exist. Has the stack been deployed? "
                f"`clamper deploy` creates one budget per limit"
            )
        else:
            message = f"could not read budget {name}: {code} - {error.get('Message') or ''}"
        return Spend(limit=limit, budget_name=name, error=message.strip())
    except BotoCoreError as exc:
        return Spend(limit=limit, budget_name=name, error=f"could not read budget {name}: {exc}")

    budget = response.get("Budget") or {}
    calculated = budget.get("CalculatedSpend") or {}
    return Spend(
        limit=limit,
        budget_name=name,
        actual=_amount(calculated.get("ActualSpend")),
        forecast=_amount(calculated.get("ForecastedSpend")),
        budget_amount=_amount(budget.get("BudgetLimit")),
        error=None
        if calculated.get("ActualSpend")
        else (
            f"budget {name} has no calculated spend yet. AWS fills it in at the next "
            f"Budgets refresh, which can take up to a day after the budget is created"
        ),
    )


def _amount(raw: Any) -> float | None:
    """`{"Amount": "123.45", "Unit": "USD"}` as a float, or `None`.

    Anything other than USD is refused rather than converted: the config is
    in USD, and comparing it against another currency would be a decision
    made on a wrong number.
    """
    if not isinstance(raw, dict):
        return None
    unit = str(raw.get("Unit") or "USD")
    if unit != "USD":
        return None
    try:
        return float(raw.get("Amount"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
