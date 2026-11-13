"""Lambda functions: reserved concurrency to zero, and put it back exactly.

Reserving zero concurrency leaves a function no concurrency to run in, so
every invocation is throttled. The function, its code, its triggers and its
configuration are untouched, which makes this the least destructive way to
stop a function in a retry loop from costing anything.

**The release has two shapes**, and this is the one handler where that
matters. The recorded previous state is either a number or *absent*:

- A function that had a reservation of, say, 20 is restored by putting 20
  back.
- A function that had **no** reservation is restored by removing the
  setting, through `release_absent` in `actions.yml` — the single named
  destructive-verb exception, `lambda:DeleteFunctionConcurrency`. Setting it
  to some number instead would permanently change how the function scales
  in the name of putting it back.

Absent is recorded as `None`, not as zero and not as a missing key. Zero is
a real reservation, a missing key is a malformed entry, and the three have
to stay distinguishable in a ledger entry somebody reads a month later.

No price. A function's cost is per request and per GB-second, and the plan
has no figure it could defend for "how much will this function cost next
hour".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from clamper.core.actions import PHASE_RELEASE, PHASE_RELEASE_ABSENT
from clamper.core.ledger import Entry
from clamper.core.resources.base import (
    Candidate,
    Handler,
    int_or_none,
    paginate,
    register,
    require_int,
)
from clamper.core.session import Clients

FIELD = "reserved_concurrent_executions"


def _not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "") == "ResourceNotFoundException"


@register
class LambdaFunctionHandler(Handler):
    action_id = "lambda-function"

    def discover(self, clients: Clients, region: str) -> list[Candidate]:
        client = clients.get("lambda", region=region)
        found: list[Candidate] = []
        for page in paginate(clients, "lambda", "list_functions", region):
            for function in page.get("Functions", []):
                name = str(function.get("FunctionName") or "")
                if not name:
                    continue
                arn = str(function.get("FunctionArn") or "")
                tags = self._tags(client, arn)
                reserved = self._reserved(client, name)
                found.append(self._candidate(function, name, tags, reserved, region))
        return found

    def _candidate(
        self,
        function: Mapping[str, Any],
        name: str,
        tags: Mapping[str, str],
        reserved: int | None,
        region: str,
    ) -> Candidate:
        runtime = str(function.get("Runtime") or function.get("PackageType") or "unknown")
        memory = int_or_none(function.get("MemorySize"))
        shape = f"{runtime}, {memory} MB" if memory else runtime
        current = "no reserved concurrency" if reserved is None else f"reserved {reserved}"
        description = f"{shape}, {current}"

        if reserved == 0:
            return self.unclampable(
                target_id=name,
                region=region,
                description=description,
                tags=tags,
                reason="reserved concurrency is already zero",
            )

        return self.candidate(
            target_id=name,
            region=region,
            description=description,
            tags=tags,
            state={FIELD: reserved},
        )

    def _tags(self, client: Any, arn: str) -> dict[str, str]:
        if not arn:
            return {}
        response = client.list_tags(Resource=arn)
        return {str(key): str(value) for key, value in (response.get("Tags") or {}).items()}

    def _reserved(self, client: Any, name: str) -> int | None:
        """The function's reservation, or `None` if it has none.

        `GetFunctionConcurrency` returns an empty body for a function with no
        reservation, rather than an error or a zero. That is the case the
        `release_absent` path exists for.
        """
        response = client.get_function_concurrency(FunctionName=name)
        return int_or_none(response.get("ReservedConcurrentExecutions"))

    def live_state(self, clients: Clients, entry: Entry) -> Mapping[str, Any] | None:
        client = clients.get("lambda", region=entry.target.region)
        try:
            reserved = self._reserved(client, entry.target.target_id)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        return {FIELD: reserved}

    def clamp_params(self, candidate: Candidate) -> dict[str, Any]:
        return {"FunctionName": candidate.target_id, "ReservedConcurrentExecutions": 0}

    def applied_state(self, candidate: Candidate) -> dict[str, Any]:
        return {FIELD: 0}

    def release_phase(self, entry: Entry) -> str:
        if FIELD not in entry.previous_state:
            # Malformed rather than absent. Raising here surfaces it as an
            # unrestorable entry; guessing "absent" would delete a
            # reservation somebody may have set on purpose.
            raise ValueError(
                f"ledger entry {entry.clamp_id}/{entry.target.key} has no "
                f"{FIELD} field. Clamper will not guess whether the function had a "
                f"reservation; the entry is reported as unrestorable."
            )
        if entry.previous_state[FIELD] is None:
            return PHASE_RELEASE_ABSENT
        return PHASE_RELEASE

    def release_params(self, entry: Entry) -> dict[str, Any]:
        if self.release_phase(entry) == PHASE_RELEASE_ABSENT:
            return {"FunctionName": entry.target.target_id}
        return {
            "FunctionName": entry.target.target_id,
            # At least 1: a recorded reservation of zero would mean the
            # function was clamped before Clamper touched it, and such a
            # candidate is never clamped in the first place.
            "ReservedConcurrentExecutions": require_int(entry, FIELD, minimum=1),
        }


__all__ = ["FIELD", "LambdaFunctionHandler"]
