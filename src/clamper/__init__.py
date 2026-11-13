"""Clamper: a hard spending limiter for AWS.

AWS Budgets tells you that you have crossed a number. Clamper is the part
that does something about it: when spend crosses a limit you set, it stops
the specific resources you allowed it to stop, records what it stopped before
it stops them, and puts everything back with one command.

Clamper writes, which the other Jet1 tools deliberately do not. Its trust
comes from somewhere else instead:

- **Opt-in.** A resource is a candidate only if you tagged it or named it.
- **Declared up front.** Every write Clamper can make is listed in
  `data/actions.yml`, and the IAM policy is generated from that list.
- **Recorded before it happens.** The previous state goes into the ledger
  before the write call, not after.
- **Reversible.** Every action in v1 has an inverse, and `clamper release`
  restores exactly what the ledger recorded.

The default mode is `dry-run`. Moving to `enforce` is a deliberate edit to
your config.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
