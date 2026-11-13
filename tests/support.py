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
