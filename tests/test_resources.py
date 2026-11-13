"""The handler registry and actions.yml have to agree.

A declared action with no registered handler would silently find nothing,
which presents to a user as "no targets matched" and is indistinguishable
from an account with nothing to clamp.

These tests import only `clamper.core.resources` — never a handler module
directly — because a handler that is registered only when somebody happens
to import its module is a handler the enforcer does not have.
"""

from __future__ import annotations

import subprocess
import sys

from clamper.core.actions import load_registry


def _registered_in_a_fresh_process() -> set[str]:
    """Registration as a cold Lambda would see it.

    A fresh interpreter, because inside the test session some other test has
    already imported every handler module and the registry would look
    complete whatever `__init__.py` says.
    """
    code = (
        "from clamper.core.resources import registered_ids;"
        "print(','.join(sorted(registered_ids())))"
    )
    output = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    ).stdout.strip()
    return set(output.split(",")) if output else set()


def test_every_declared_action_has_a_registered_handler() -> None:
    """Equality, not a subset. A missing handler is an action that silently
    finds nothing; an extra one is a handler for an action nobody declared."""
    assert _registered_in_a_fresh_process() == load_registry().ids()
