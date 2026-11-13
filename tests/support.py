"""Constants shared between the fixtures and the tests.

Separate from `conftest.py` so tests can import them directly. pytest adds
the test directory to `sys.path`, so `from support import REGION` works
without making `tests/` a package — and importing from `conftest` instead
depends on pytest internals that have changed before.
"""

from __future__ import annotations

REGION = "us-east-1"
OTHER_REGION = "eu-west-2"

#: AWS's documentation placeholder. Not a real account, and the only account
#: id that appears anywhere in this repository.
ACCOUNT = "123456789012"

TABLE = "clamper-ledger"
STACK = "clamper"
TOPIC_ARN = f"arn:aws:sns:{REGION}:{ACCOUNT}:clamper-notifications"
