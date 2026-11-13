"""Reading spend from Budgets, and deciding from it.

Budgets is stubbed with botocore's Stubber, which validates each request
against the real service model: a wrong parameter name fails here rather
than in somebody's account.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from support import ACCOUNT, STACK

from clamper.core.budgets import Spend, read_spend
from clamper.core.config import Limit
from clamper.core.decide import LEVEL_CLAMP, LEVEL_OK, LEVEL_UNKNOWN, LEVEL_WARN, decide
from clamper.core.session import Clients

LIMIT = Limit(id="whole-account", amount_usd=500.0)


class _Session:
    region_name = "us-east-1"


def stubbed(response: dict[str, Any] | None = None, *, error: str | None = None) -> Clients:
    client = boto3.client("budgets", region_name="us-east-1")
    stubber = Stubber(client)
    expected = {"AccountId": ACCOUNT, "BudgetName": f"{STACK}-whole-account"}
    if error:
        stubber.add_client_error(
            "describe_budget", service_error_code=error, expected_params=expected
        )
    else:
        stubber.add_response("describe_budget", response or {}, expected)
    stubber.activate()
    clients = Clients(_Session())
    clients.stub("budgets", client)
    return clients


def budget(actual: str | None, forecast: str | None = None, limit: str = "500") -> dict[str, Any]:
    calculated: dict[str, Any] = {}
    if actual is not None:
        calculated["ActualSpend"] = {"Amount": actual, "Unit": "USD"}
    if forecast is not None:
        calculated["ForecastedSpend"] = {"Amount": forecast, "Unit": "USD"}
    shape: dict[str, Any] = {
        "BudgetName": f"{STACK}-whole-account",
        "BudgetLimit": {"Amount": limit, "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    }
    # The service model makes ActualSpend required inside CalculatedSpend,
    # so a budget with nothing calculated yet has no CalculatedSpend at all.
    if calculated:
        shape["CalculatedSpend"] = calculated
    return {"Budget": shape}


# --- reading ------------------------------------------------------------


def test_actual_and_forecast_are_read() -> None:
    spend = read_spend(stubbed(budget("123.45", "480.00")), ACCOUNT, LIMIT, STACK)
    assert spend.actual == 123.45
    assert spend.forecast == 480.0
    assert spend.known
    assert spend.mismatch is None


def test_a_missing_forecast_is_none_not_zero() -> None:
    """AWS needs about five weeks of history to forecast. A zero forecast
    and an absent one mean opposite things."""
    spend = read_spend(stubbed(budget("10.00")), ACCOUNT, LIMIT, STACK)
    assert spend.forecast is None


def test_a_missing_budget_says_to_deploy() -> None:
    spend = read_spend(stubbed(error="NotFoundException"), ACCOUNT, LIMIT, STACK)
    assert not spend.known
    assert "clamper deploy" in (spend.error or "")


def test_access_denied_is_unknown_not_zero() -> None:
    spend = read_spend(stubbed(error="AccessDeniedException"), ACCOUNT, LIMIT, STACK)
    assert spend.actual is None
    assert "AccessDeniedException" in (spend.error or "")


def test_a_budget_with_no_calculated_spend_yet_is_unknown() -> None:
    spend = read_spend(stubbed(budget(None)), ACCOUNT, LIMIT, STACK)
    assert not spend.known
    assert "refresh" in (spend.error or "")


def test_a_hand_edited_budget_is_reported() -> None:
    spend = read_spend(stubbed(budget("1.00", limit="9000")), ACCOUNT, LIMIT, STACK)
    assert "clamper deploy" in (spend.mismatch or "")


def test_a_non_usd_amount_is_refused_rather_than_compared() -> None:
    response = budget("1.00")
    response["Budget"]["CalculatedSpend"]["ActualSpend"]["Unit"] = "EUR"
    spend = read_spend(stubbed(response), ACCOUNT, LIMIT, STACK)
    assert spend.actual is None


# --- deciding -----------------------------------------------------------


def spend(actual: float | None, forecast: float | None = None, **limit: Any) -> Spend:
    chosen = Limit(id="whole-account", amount_usd=500.0, **limit)
    return Spend(
        limit=chosen,
        budget_name="clamper-whole-account",
        actual=actual,
        forecast=forecast,
        error=None if actual is not None else "unreadable",
    )


@pytest.mark.parametrize(
    ("actual", "level"),
    [
        (0.0, LEVEL_OK),
        (399.99, LEVEL_OK),
        (400.0, LEVEL_WARN),
        (499.99, LEVEL_WARN),
        (500.0, LEVEL_CLAMP),
        (9000.0, LEVEL_CLAMP),
    ],
)
def test_thresholds_are_inclusive_percentages(actual: float, level: str) -> None:
    assert decide(spend(actual)).level == level


def test_unreadable_spend_is_unknown_never_ok() -> None:
    decision = decide(spend(None))
    assert decision.level == LEVEL_UNKNOWN
    assert decision.unknown
    assert not decision.breached


def test_a_forecast_does_not_clamp_an_actual_limit() -> None:
    """The default type is actual. A high forecast is not a breach."""
    assert decide(spend(100.0, forecast=9000.0)).level == LEVEL_OK


def test_a_forecasted_limit_clamps_on_the_forecast_and_says_so() -> None:
    decision = decide(spend(100.0, forecast=600.0, notification_type="forecasted"))
    assert decision.level == LEVEL_CLAMP
    assert decision.basis == "forecasted"
    assert "forecast" in decision.reason


def test_a_forecasted_limit_with_no_forecast_falls_back_to_actual() -> None:
    """On a young account a forecast-only limit would never fire, and nobody
    could tell that from "nothing has gone wrong"."""
    over = decide(spend(600.0, forecast=None, notification_type="forecasted"))
    assert over.level == LEVEL_CLAMP
    assert over.basis == "actual"

    under = decide(spend(450.0, forecast=None, notification_type="forecasted"))
    assert under.level == LEVEL_WARN
    assert "no forecast yet" in under.reason


def test_a_clamp_threshold_above_100_is_honoured() -> None:
    assert decide(spend(550.0, clamp_percent=120.0)).level == LEVEL_WARN
    assert decide(spend(600.0, clamp_percent=120.0)).level == LEVEL_CLAMP
