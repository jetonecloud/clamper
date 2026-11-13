"""The only module that performs a mutating AWS call.

Everything else in Clamper — the resource handlers, the enforcer, the CLI —
builds a `Call` and hands it here. That is the whole of the first safety
layer, and it works because of what a `Call` does *not* contain:

**A `Call` cannot name a method.** It names an action id and a phase. The
actuator looks the boto3 method up in the registry, which is loaded from
`actions.yml`, which refuses destructive verbs at load. So there is no value
a caller can pass — not a typo, not a crafted ledger entry, not a config key
— that reaches `getattr` as something other than a declared, reversible
call.

The checks, in the order they run:

1. The action id exists. An unknown one is `UnknownAction`.
2. The phase exists for that action. `release_absent` on an action that has
   none is `UnknownAction`.
3. The resolved method is in the allowlist derived from the registry.
   Belt and braces over (1) and (2), and the thing `test_safety.py` pins.
4. The parameters are exactly the action's declared parameter names. An
   extra key is an error, because `Force=True` arriving as an unchecked
   passthrough is the shape of this entire class of bug. A missing one is an
   error too, because a partial `update_auto_scaling_group` leaves a group
   in a state nobody designed.
5. If `dry_run`, return without touching boto3. One `if`, at the top, rather
   than a flag each handler is trusted to honour.

A call that AWS rejects comes back as `Result(ok=False)` with the error code
and message attached. It is never raised, because one target Clamper cannot
stop must not stop it trying the other nineteen — and must never be counted
as done. Programming errors (1 to 4 above) *are* raised: those are bugs in
Clamper, not conditions in somebody's account.

Importable inside a Lambda.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actions import PHASE_CLAMP, Registry, load_registry
from clamper.core.session import Clients


class ForbiddenCall(RuntimeError):
    """A call that is not a declared, reversible action.

    Raised rather than returned. A `Result(ok=False)` means "AWS said no",
    which is a fact about an account; this means "Clamper tried to do
    something it is not allowed to do", which is a bug and should stop the
    run loudly.
    """


@dataclass(frozen=True)
class Call:
    """One mutating AWS call, described by what it is rather than how.

    `target_id` and `region` are carried for the report and the ledger. They
    are never used to build the request — the identifier always arrives
    inside `params`, where it is checked against the action's declared
    parameter names.
    """

    action_id: str
    phase: str
    params: Mapping[str, Any]
    target_id: str
    region: str | None = None

    @property
    def label(self) -> str:
        return f"{self.action_id}.{self.phase} on {self.target_id}"


@dataclass(frozen=True)
class Result:
    """What happened. Never optimistic.

    `ok` is true only when AWS accepted the call, or when the call was
    deliberately not made because of `dry_run`. Those two are distinguished
    by `dry_run` rather than conflated, because "would have worked" and
    "worked" are different claims and only one of them stops the spend.
    """

    call: Call
    ok: bool
    dry_run: bool = False
    error_code: str | None = None
    error_message: str | None = None
    #: The AWS response, minus its metadata. Used to read back what a call
    #: actually applied; never persisted, because a response can be large and
    #: a ledger entry should be a few hundred bytes.
    response: dict[str, Any] = field(default_factory=dict)

    @property
    def performed(self) -> bool:
        """The call was really made and really accepted.

        The property every counter uses. `ok` alone would count a dry run as
        a clamp, which is the exact misreport the threat model calls a
        security issue.
        """
        return self.ok and not self.dry_run

    @property
    def summary(self) -> str:
        if self.dry_run:
            return "would clamp (dry-run)"
        if self.ok:
            return "done"
        return f"failed: {self.error_code or 'error'} - {self.error_message or 'no detail'}"


class Actuator:
    """The write path. One per run.

    `dry_run` is set from the config once, at construction, and there is no
    setter. A mode that can be changed mid-run is a mode that can be changed
    by a bug.
    """

    def __init__(
        self,
        clients: Clients,
        *,
        dry_run: bool,
        registry: Registry | None = None,
    ) -> None:
        self._clients = clients
        self._dry_run = bool(dry_run)
        self._registry = registry if registry is not None else load_registry()
        #: Derived, never written out by hand. `test_safety.py` asserts this
        #: equals the registry's method set and pins the set itself.
        self._allowed_methods = self._registry.methods
        self._performed: list[Result] = []

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def allowed_methods(self) -> frozenset[str]:
        return self._allowed_methods

    @property
    def performed(self) -> tuple[Result, ...]:
        """Every call that was really made, in order.

        Kept so a run can report what it did even if it crashes partway, and
        so a test can assert a dry run made none.
        """
        return tuple(self._performed)

    def invoke(self, call: Call) -> Result:
        """Check, then make, one declared call."""
        action = self._registry.get(call.action_id)
        operation = action.operation(call.phase)

        # Layer three. (1) and (2) already guarantee this, which is the
        # point: if a future change to the registry lets an undeclared
        # method through, it fails here instead of reaching AWS.
        if operation.method not in self._allowed_methods:
            raise ForbiddenCall(
                f"{operation.method} is not in the allowlist built from actions.yml. "
                f"Allowed: {', '.join(sorted(self._allowed_methods))}."
            )

        self._check_params(call, operation.params, operation.method)

        if self._dry_run:
            # Nothing below this line runs in dry-run. No client is fetched,
            # no credential is resolved, no request is signed.
            return Result(call=call, ok=True, dry_run=True)

        client = self._clients.get(action.client, region=call.region)
        method = getattr(client, operation.method)

        try:
            raw = method(**dict(call.params))
        except ClientError as exc:
            result = Result(
                call=call,
                ok=False,
                error_code=_code(exc),
                error_message=_message(exc),
            )
        except BotoCoreError as exc:
            # Connection, endpoint, parameter-validation errors. Still a
            # failure of this call rather than of the run.
            result = Result(
                call=call,
                ok=False,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        else:
            response = {
                key: value for key, value in (raw or {}).items() if key != "ResponseMetadata"
            }
            result = Result(call=call, ok=True, response=response)

        self._performed.append(result)
        return result

    def _check_params(self, call: Call, declared: tuple[str, ...], method: str) -> None:
        """Exactly the declared parameters, no more and no fewer."""
        given = set(call.params)
        expected = set(declared)

        extra = sorted(given - expected)
        if extra:
            raise ForbiddenCall(
                f"{call.label} passes undeclared parameter(s) {', '.join(extra)} to "
                f"{method}. actions.yml declares {', '.join(declared)}. A parameter "
                f"the declaration does not mention is a parameter nobody reviewed."
            )

        missing = sorted(expected - given)
        if missing:
            raise ForbiddenCall(
                f"{call.label} omits declared parameter(s) {', '.join(missing)} for "
                f"{method}. A partial update leaves the resource in a state nobody "
                f"designed — Auto Scaling in particular, where omitting MinSize on a "
                f"release restores the desired capacity under a minimum of zero."
            )


def clamp_call(action_id: str, target_id: str, params: Mapping[str, Any], region: str) -> Call:
    """A clamp call. A convenience, and the only place the phase is defaulted."""
    return Call(
        action_id=action_id,
        phase=PHASE_CLAMP,
        params=params,
        target_id=target_id,
        region=region,
    )


def _code(exc: ClientError) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "ClientError")
    return "ClientError"


def _message(exc: ClientError) -> str:
    """The error text, trimmed.

    AWS error messages occasionally quote a request, and a ledger entry and a
    notification are both places a long one would be unhelpful. Trimmed
    rather than dropped: the reason a clamp failed is the most useful thing
    in a failure report.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            text = str(error.get("Message") or "").strip()
            if text:
                return text[:500]
    return str(exc)[:500]
