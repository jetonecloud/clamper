"""The names of Clamper's own resources, derived from the stack name.

One function, used by three things that must agree: the template generator,
which names the resources; the enforcer, which reads them from its
environment; and self-exclusion, which refuses to clamp any of them. A name
that was configurable separately would be one more thing that could
disagree, and the disagreement that matters is "the enforcer did not
recognise its own function and set its concurrency to zero".

Importable inside a Lambda.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StackNames:
    stack_name: str

    @property
    def function(self) -> str:
        return f"{self.stack_name}-enforcer"

    @property
    def table(self) -> str:
        return f"{self.stack_name}-ledger"

    @property
    def topic(self) -> str:
        return f"{self.stack_name}-notifications"

    @property
    def role(self) -> str:
        return f"{self.stack_name}-enforcer"

    @property
    def schedule(self) -> str:
        return f"{self.stack_name}-reconcile"

    @property
    def log_group(self) -> str:
        return f"/aws/lambda/{self.function}"

    @property
    def bootstrap_stack(self) -> str:
        return f"{self.stack_name}-bootstrap"

    @property
    def all(self) -> frozenset[str]:
        """Every name, for the by-name self-exclusion check."""
        return frozenset({self.function, self.table, self.topic, self.role, self.schedule})
