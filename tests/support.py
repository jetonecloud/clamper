"""Constants shared between the fixtures and the tests.

Separate from `conftest.py` so tests can import them directly. pytest adds
the test directory to `sys.path`, so `from support import REGION` works
without making `tests/` a package — and importing from `conftest` instead
depends on pytest internals that have changed before.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

REGION = "us-east-1"
OTHER_REGION = "eu-west-2"

#: AWS's documentation placeholder. Not a real account, and the only account
#: id that appears anywhere in this repository.
ACCOUNT = "123456789012"

TABLE = "clamper-ledger"
STACK = "clamper"
TOPIC_ARN = f"arn:aws:sns:{REGION}:{ACCOUNT}:clamper-notifications"


def spend_reader(amount: float | None) -> Callable[..., Any]:
    """A stand-in for `budgets.read_spend` with a fixed actual spend."""
    from clamper.core.budgets import Spend

    def read(clients: Any, account: str, limit: Any, stack_name: str) -> Any:
        return Spend(
            limit=limit,
            budget_name=limit.budget_name(stack_name),
            actual=amount,
            error=None if amount is not None else "AccessDeniedException - nope",
        )

    return read


def record_clamp(
    ledger: Any,
    target_ids: tuple[str, ...] = ("i-0123456789abcdef0",),
    *,
    action_id: str = "ec2-instance",
    clamp_id: str = "20261113t153000-abcd",
    settle: bool = True,
    previous: dict[str, Any] | None = None,
    applied: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """A clamp in the ledger, as an enforce-mode run would leave it.

    `settle=False` leaves the entries pending, the state a run that died
    between record and settle leaves behind.
    """
    from clamper.core.actions import load_registry
    from clamper.core.actuator import Call, Result
    from clamper.core.ledger import Clamp, Target

    title = load_registry().get(action_id).title
    ledger.open_clamp(
        Clamp(
            clamp_id=clamp_id,
            created_at="2026-11-13T15:30:00+00:00",
            mode="enforce",
            actor="test",
            limit_id="whole-account",
            limit_amount=100.0,
            spend=150.0,
            reason="actual $150.00 is 150% of $100.00",
            stack_name=STACK,
        )
    )
    entries = []
    for target_id in target_ids:
        entry = ledger.record(
            clamp_id,
            Target(action_id=action_id, target_id=target_id, region=REGION, title=title),
            previous if previous is not None else {"state": "running"},
            applied if applied is not None else {"state": "stopped"},
        )
        if settle:
            call = Call(action_id, "clamp", {}, target_id, REGION)
            entry = ledger.settle(entry, Result(call=call, ok=True))
        entries.append(entry)
    return tuple(entries)
