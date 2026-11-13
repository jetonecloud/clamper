"""One handler per row in `actions.yml`.

Importing this package registers every handler. The import list below and
`actions.yml` have to agree, and `tests/test_resources.py` asserts they do:
a declared action with no handler would silently find nothing, which presents
to a user as "no targets matched" and is indistinguishable from an account
with nothing to clamp.
"""

from __future__ import annotations

from clamper.core.resources import (
    asg,  # noqa: F401  - imported to register
    ec2,  # noqa: F401  - imported to register
)
from clamper.core.resources.base import (
    Candidate,
    Handler,
    handler_for,
    handlers_for,
    registered_ids,
)

__all__ = [
    "Candidate",
    "Handler",
    "handler_for",
    "handlers_for",
    "registered_ids",
]
