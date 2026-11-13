"""The ledger: what was changed, what it was before, and whether it worked.

This is what makes Clamper reversible, so two properties matter more than
anything else in the module.

**It is write-ahead.** `record()` stores the previous state *before* the
actuator makes the call. The orderings that were considered and rejected:

- *Call, then record.* The window is small and the consequence is total: a
  process killed between the two leaves a stopped resource with no record of
  what it was, and `clamper release` cannot restore what it does not know.
- *Record and call in a transaction.* There is no such thing across DynamoDB
  and EC2.
- *Record `clamped` up front and correct it on failure.* Then a crash
  mid-run leaves entries claiming success for calls that never happened, and
  `release` tries to start instances that were never stopped.

So a crash leaves `PENDING` entries, and `PENDING` means **outcome
unknown**. It is never counted as clamped and never rendered as clean. The
next reconcile re-reads the resource's live state and settles the entry from
what it finds.

**It is never pruned.** No TTL attribute, no `clamper prune`, and
`delete_item` appears nowhere — `tests/test_safety.py` asserts that
structurally, because this is the record of every time this tool changed
something in somebody's account and an audit log that deletes itself after
30 days is not one. It is tiny: a few hundred bytes per target, against
DynamoDB's 25 GB free tier.

Two states are recorded per entry, and both are needed:

- `previous_state` is what to restore.
- `applied_state` is what Clamper set it to, which is how release detects a
  conflict. Without it, "the instance is running" could mean "somebody
  started it" or "the clamp failed", and those need opposite handling.

Importable inside a Lambda.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

# A pure data converter: it maps Python values to and from DynamoDB's
# attribute-value shape and has no client, session or network surface.
# `tests/test_safety.py` allows this one import by exact module path, and
# nothing else from boto3 outside core/session.py.
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.actuator import Result
from clamper.core.session import Clients

#: The header item for a clamp run. A sort key that no target key can
#: collide with, because `#` is not legal in any AWS resource identifier.
CLAMP_SORT_KEY = "#clamp"

STATUS_PENDING = "pending"
STATUS_CLAMPED = "clamped"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry-run"
STATUS_RELEASED = "released"
STATUS_RELEASE_FAILED = "release-failed"
STATUS_SKIPPED = "skipped"

STATUSES = (
    STATUS_PENDING,
    STATUS_CLAMPED,
    STATUS_FAILED,
    STATUS_DRY_RUN,
    STATUS_RELEASED,
    STATUS_RELEASE_FAILED,
    STATUS_SKIPPED,
)

#: Statuses that mean a resource is currently stopped by Clamper and has
#: something to restore. The only input to `clamper release --all`.
ACTIVE_STATUSES = (STATUS_CLAMPED,)

#: Statuses where Clamper does not know whether the resource is stopped.
#: Reported separately from both success and failure, and they make the exit
#: code 2 — a hole in the enforcement rather than a finding in a report.
UNKNOWN_STATUSES = (STATUS_PENDING,)

#: The GSI the stack creates, so "what is clamped right now" is a query
#: rather than a full-table scan.
STATUS_INDEX = "by-status"

CLAMP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Where the enforcer finds its table. Set by the stack; never a default,
#: because a Lambda that silently wrote to `clamper-ledger` in the wrong
#: account is worse than one that fails to start.
TABLE_ENV = "CLAMPER_LEDGER_TABLE"

_SERIALISER = TypeSerializer()
_DESERIALISER = TypeDeserializer()


class LedgerError(RuntimeError):
    """The ledger is unusable.

    Raised, not swallowed. A clamp that cannot be recorded must not happen:
    an unrecorded clamp is an unreleasable one, which is the one failure this
    whole design exists to prevent. `enforcer/run.py` aborts the run.
    """


@dataclass(frozen=True)
class Target:
    """The identity of something that may be clamped.

    Deliberately not the resource itself. The ledger stores what it needs to
    build a release call later and nothing else, so an entry stays small and
    carries no attribute somebody might not want written down.
    """

    action_id: str
    target_id: str
    region: str
    title: str
    #: Free-text, for the report only. Never used to build a call.
    description: str = ""

    @property
    def key(self) -> str:
        """The sort key. Stable, so a second clamp of the same resource in
        the same run overwrites rather than duplicating."""
        return f"{self.action_id}/{self.region}/{self.target_id}"


@dataclass(frozen=True)
class Entry:
    """One target, within one clamp run."""

    clamp_id: str
    target: Target
    status: str
    previous_state: Mapping[str, Any]
    applied_state: Mapping[str, Any]
    created_at: str
    updated_at: str
    dry_run: bool = False
    error_code: str | None = None
    error_message: str | None = None
    #: Why this entry was skipped, for `STATUS_SKIPPED`. Always set when it
    #: is, because a target nobody can explain is a target nobody trusts.
    skip_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def outcome_unknown(self) -> bool:
        return self.status in UNKNOWN_STATUSES

    @property
    def clamped(self) -> bool:
        """Really stopped. Not a dry run, not pending, not failed."""
        return self.status == STATUS_CLAMPED

    @property
    def label(self) -> str:
        return f"{self.target.title} {self.target.target_id}"

    def to_item(self) -> dict[str, Any]:
        return {
            "pk": self.clamp_id,
            "sk": self.target.key,
            "kind": "entry",
            "status": self.status,
            "clamp_id": self.clamp_id,
            "action_id": self.target.action_id,
            "target_id": self.target.target_id,
            "region": self.target.region,
            "title": self.target.title,
            "description": self.target.description,
            "previous_state": dict(self.previous_state),
            "applied_state": dict(self.applied_state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dry_run": self.dry_run,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "skip_reason": self.skip_reason,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> Entry:
        return cls(
            clamp_id=str(item["clamp_id"]),
            target=Target(
                action_id=str(item["action_id"]),
                target_id=str(item["target_id"]),
                region=str(item["region"]),
                title=str(item.get("title") or item["action_id"]),
                description=str(item.get("description") or ""),
            ),
            status=str(item["status"]),
            previous_state=dict(item.get("previous_state") or {}),
            applied_state=dict(item.get("applied_state") or {}),
            created_at=str(item["created_at"]),
            updated_at=str(item.get("updated_at") or item["created_at"]),
            dry_run=bool(item.get("dry_run")),
            error_code=_optional_str(item.get("error_code")),
            error_message=_optional_str(item.get("error_message")),
            skip_reason=_optional_str(item.get("skip_reason")),
        )


@dataclass(frozen=True)
class Clamp:
    """The header for one clamp run: why it happened and who caused it."""

    clamp_id: str
    created_at: str
    mode: str
    actor: str
    limit_id: str
    limit_amount: float
    spend: float
    reason: str
    stack_name: str = ""
    entries: tuple[Entry, ...] = field(default_factory=tuple)

    @property
    def dry_run(self) -> bool:
        return self.mode != "enforce"

    @property
    def clamped(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.clamped)

    @property
    def not_clamped(self) -> tuple[Entry, ...]:
        """Everything that is not a confirmed stop, for whatever reason.

        One property rather than three, because every report has to show
        them together: a failure, an unknown outcome and a skip are all
        "this is still costing you money".
        """
        return tuple(entry for entry in self.entries if not entry.clamped)

    @property
    def unknown(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.outcome_unknown)

    @property
    def active(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.active)

    @property
    def release_command(self) -> str:
        """What to paste to undo this.

        In every notification, because the person reading it at 3am did not
        write the config and needs the shortest path from "something
        stopped" to "I have put it back".
        """
        return f"clamper release {self.clamp_id}"

    def to_item(self) -> dict[str, Any]:
        return {
            "pk": self.clamp_id,
            "sk": CLAMP_SORT_KEY,
            "kind": "clamp",
            # The header carries a status too, so the GSI can answer "which
            # runs happened" without a scan. It is always `clamped`: the
            # header records that a run occurred, not whether it worked.
            "status": STATUS_CLAMPED if not self.dry_run else STATUS_DRY_RUN,
            "clamp_id": self.clamp_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "actor": self.actor,
            "limit_id": self.limit_id,
            "limit_amount": self.limit_amount,
            "spend": self.spend,
            "reason": self.reason,
            "stack_name": self.stack_name,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, Any], entries: tuple[Entry, ...] = ()) -> Clamp:
        return cls(
            clamp_id=str(item["clamp_id"]),
            created_at=str(item["created_at"]),
            mode=str(item.get("mode") or "dry-run"),
            actor=str(item.get("actor") or "unknown"),
            limit_id=str(item.get("limit_id") or ""),
            limit_amount=float(item.get("limit_amount") or 0),
            spend=float(item.get("spend") or 0),
            reason=str(item.get("reason") or ""),
            stack_name=str(item.get("stack_name") or ""),
            entries=entries,
        )


class LedgerStore(Protocol):
    """What the enforcer and the CLI need from a ledger.

    A protocol so `clamper simulate` can run the real enforcer logic against
    `MemoryLedger` and need no AWS account at all. The thing being tested is
    the decision, and a decision that only works against DynamoDB is one
    nobody can try out.
    """

    def open_clamp(self, clamp: Clamp) -> Clamp: ...

    def record(
        self,
        clamp_id: str,
        target: Target,
        previous_state: Mapping[str, Any],
        applied_state: Mapping[str, Any],
    ) -> Entry: ...

    def settle(self, entry: Entry, result: Result) -> Entry: ...

    def skip(self, clamp_id: str, target: Target, reason: str) -> Entry: ...

    def mark_released(self, entry: Entry, result: Result) -> Entry: ...

    def entries_for(self, clamp_id: str) -> tuple[Entry, ...]: ...

    def clamp(self, clamp_id: str) -> Clamp | None: ...

    def active(self) -> tuple[Entry, ...]: ...

    def history(self, limit: int = 50) -> tuple[Clamp, ...]: ...


def new_clamp_id(*, now: datetime | None = None) -> str:
    """A sortable, readable, unguessable-enough clamp id.

    Time first so a lexical sort is a chronological one, which is what makes
    the DynamoDB range query useful. Six random hex characters because two
    runs in the same second are possible — an SNS notification and a
    scheduled reconcile arriving together — and a collision would merge two
    runs into one ledger entry set.

    Lowercase and hyphens only: this string is printed in a notification
    inside a `clamper release <id>` command somebody will paste into a
    shell.
    """
    moment = now or datetime.now(UTC)
    return f"{moment.strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(3)}"


def table_name_from_env() -> str:
    """The enforcer's table name, from the stack.

    No default. A Lambda that silently wrote to `clamper-ledger` in the
    wrong account is worse than one that fails to start.
    """
    name = os.environ.get(TABLE_ENV, "").strip()
    if not name:
        raise LedgerError(
            f"{TABLE_ENV} is not set. The enforcer gets its ledger table name from "
            f"the stack, and there is deliberately no default: a function that "
            f"guessed a table name could write a clamp record somewhere nobody "
            f"would look for it."
        )
    return name


class Ledger:
    """The DynamoDB ledger.

    Two write calls, `put_item` and `update_item`, and no delete. That is
    pinned by `tests/test_safety.py`, which is how "the ledger is never
    pruned" is a property of the code rather than a promise in a document.
    """

    def __init__(
        self,
        clients: Clients,
        table_name: str,
        *,
        region: str | None = None,
    ) -> None:
        self._client = clients.get("dynamodb", region=region)
        self._table = table_name

    @property
    def table_name(self) -> str:
        return self._table

    # --- writing --------------------------------------------------------

    def open_clamp(self, clamp: Clamp) -> Clamp:
        """Record that a run is starting, before any target is touched."""
        self._put(clamp.to_item())
        return clamp

    def record(
        self,
        clamp_id: str,
        target: Target,
        previous_state: Mapping[str, Any],
        applied_state: Mapping[str, Any],
    ) -> Entry:
        """Write-ahead. The previous state goes down before the call is made.

        Returns a `PENDING` entry. If the process dies here, the ledger says
        "we were about to stop this and do not know what happened", which is
        the only honest thing it could say.
        """
        now = _now()
        entry = Entry(
            clamp_id=clamp_id,
            target=target,
            status=STATUS_PENDING,
            previous_state=previous_state,
            applied_state=applied_state,
            created_at=now,
            updated_at=now,
        )
        self._put(entry.to_item())
        return entry

    def settle(self, entry: Entry, result: Result) -> Entry:
        """Record what the call actually did. Never optimistic.

        A dry run settles to `DRY_RUN`, not `CLAMPED`. They are different
        facts and only one of them stopped any spending.
        """
        if result.dry_run:
            status = STATUS_DRY_RUN
        elif result.ok:
            status = STATUS_CLAMPED
        else:
            status = STATUS_FAILED

        settled = _replace(
            entry,
            status=status,
            updated_at=_now(),
            dry_run=result.dry_run,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        self._put(settled.to_item())
        return settled

    def skip(self, clamp_id: str, target: Target, reason: str) -> Entry:
        """Record a target Clamper decided not to touch, and why.

        Skips are written to the ledger rather than only reported, so
        "nothing was clamped" and "forty things were skipped for a reason
        nobody wrote down" are distinguishable a month later.
        """
        now = _now()
        entry = Entry(
            clamp_id=clamp_id,
            target=target,
            status=STATUS_SKIPPED,
            previous_state={},
            applied_state={},
            created_at=now,
            updated_at=now,
            skip_reason=reason,
        )
        self._put(entry.to_item())
        return entry

    def mark_released(self, entry: Entry, result: Result) -> Entry:
        """Record the outcome of a release.

        A failed release stays visible as `RELEASE_FAILED` rather than
        reverting to `CLAMPED`, because "we tried to put this back and could
        not" is a different thing to tell somebody than "this is still
        stopped".
        """
        released = _replace(
            entry,
            status=STATUS_RELEASED if result.performed else STATUS_RELEASE_FAILED,
            updated_at=_now(),
            dry_run=result.dry_run,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        self._put(released.to_item())
        return released

    # --- reading --------------------------------------------------------

    def entries_for(self, clamp_id: str) -> tuple[Entry, ...]:
        # Just `pk = :pk`, with the header filtered in Python. A key
        # condition of `sk <> :header` is the obvious-looking version and is
        # invalid: DynamoDB's sort key supports only =, <, <=, >, >=, BETWEEN
        # and begins_with, so it fails at runtime rather than at review.
        items = self._query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues=_encode({":pk": clamp_id}),
        )
        return tuple(Entry.from_item(item) for item in items if item.get("sk") != CLAMP_SORT_KEY)

    def clamp(self, clamp_id: str) -> Clamp | None:
        if not CLAMP_ID.match(clamp_id):
            raise LedgerError(
                f"{clamp_id!r} is not a clamp id. They look like "
                f"`20261113t153000z-a1b2c3`; `clamper ledger` lists them."
            )
        items = self._query(
            KeyConditionExpression="pk = :pk AND sk = :sk",
            ExpressionAttributeValues=_encode({":pk": clamp_id, ":sk": CLAMP_SORT_KEY}),
        )
        if not items:
            return None
        return Clamp.from_item(items[0], self.entries_for(clamp_id))

    def active(self) -> tuple[Entry, ...]:
        """Everything currently stopped by Clamper, newest first.

        A GSI query rather than a scan, because a scan over a ledger that is
        never pruned gets slower every month and this is the query
        `clamper status` makes every time.
        """
        found: list[Entry] = []
        for status in ACTIVE_STATUSES:
            for item in self._query(
                IndexName=STATUS_INDEX,
                KeyConditionExpression="#s = :status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=_encode({":status": status}),
                ScanIndexForward=False,
            ):
                if item.get("sk") != CLAMP_SORT_KEY:
                    found.append(Entry.from_item(item))
        return tuple(sorted(found, key=lambda entry: entry.updated_at, reverse=True))

    def history(self, limit: int = 50) -> tuple[Clamp, ...]:
        """Clamp headers, newest first, without their entries.

        Entries are fetched per clamp on demand. A history of two hundred
        runs is cheap; two hundred runs' worth of entries is not, and
        `clamper ledger` shows one line per run.
        """
        headers: list[Clamp] = []
        for status in (STATUS_CLAMPED, STATUS_DRY_RUN):
            for item in self._query(
                IndexName=STATUS_INDEX,
                KeyConditionExpression="#s = :status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=_encode({":status": status}),
                ScanIndexForward=False,
            ):
                if item.get("sk") == CLAMP_SORT_KEY:
                    headers.append(Clamp.from_item(item))
        headers.sort(key=lambda clamp: clamp.created_at, reverse=True)
        return tuple(headers[:limit])

    # --- the two DynamoDB calls -----------------------------------------

    def _put(self, item: Mapping[str, Any]) -> None:
        client = self._client
        try:
            client.put_item(TableName=self._table, Item=_encode(item))
        except (ClientError, BotoCoreError) as exc:
            raise LedgerError(
                f"Could not write to the ledger table {self._table}: {exc}\n"
                f"Clamper does not act on a target it could not record first, so "
                f"this run is stopping. Nothing has been clamped."
            ) from exc

    def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        client = self._client
        items: list[dict[str, Any]] = []
        token: dict[str, Any] | None = None
        try:
            while True:
                request = dict(kwargs, TableName=self._table)
                if token:
                    request["ExclusiveStartKey"] = token
                page = client.query(**request)
                items.extend(_decode(raw) for raw in page.get("Items", []))
                token = page.get("LastEvaluatedKey")
                if not token:
                    break
        except (ClientError, BotoCoreError) as exc:
            raise LedgerError(f"Could not read the ledger table {self._table}: {exc}") from exc
        return items


class MemoryLedger:
    """An in-memory ledger, for `clamper simulate` and the tests.

    Exists so the real enforcer logic can be run with no AWS account at all.
    `clamper simulate` forces dry-run and uses this, which means the thing a
    user tries out is the code that will run in their account rather than a
    separate demo path that could drift from it.
    """

    def __init__(self) -> None:
        self._clamps: dict[str, Clamp] = {}
        self._entries: dict[tuple[str, str], Entry] = {}

    @property
    def table_name(self) -> str:
        return "(in memory)"

    def open_clamp(self, clamp: Clamp) -> Clamp:
        self._clamps[clamp.clamp_id] = clamp
        return clamp

    def record(
        self,
        clamp_id: str,
        target: Target,
        previous_state: Mapping[str, Any],
        applied_state: Mapping[str, Any],
    ) -> Entry:
        now = _now()
        entry = Entry(
            clamp_id=clamp_id,
            target=target,
            status=STATUS_PENDING,
            previous_state=dict(previous_state),
            applied_state=dict(applied_state),
            created_at=now,
            updated_at=now,
        )
        self._entries[(clamp_id, target.key)] = entry
        return entry

    def settle(self, entry: Entry, result: Result) -> Entry:
        if result.dry_run:
            status = STATUS_DRY_RUN
        elif result.ok:
            status = STATUS_CLAMPED
        else:
            status = STATUS_FAILED
        settled = _replace(
            entry,
            status=status,
            updated_at=_now(),
            dry_run=result.dry_run,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        self._entries[(entry.clamp_id, entry.target.key)] = settled
        return settled

    def skip(self, clamp_id: str, target: Target, reason: str) -> Entry:
        now = _now()
        entry = Entry(
            clamp_id=clamp_id,
            target=target,
            status=STATUS_SKIPPED,
            previous_state={},
            applied_state={},
            created_at=now,
            updated_at=now,
            skip_reason=reason,
        )
        self._entries[(clamp_id, target.key)] = entry
        return entry

    def mark_released(self, entry: Entry, result: Result) -> Entry:
        released = _replace(
            entry,
            status=STATUS_RELEASED if result.performed else STATUS_RELEASE_FAILED,
            updated_at=_now(),
            dry_run=result.dry_run,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        self._entries[(entry.clamp_id, entry.target.key)] = released
        return released

    def entries_for(self, clamp_id: str) -> tuple[Entry, ...]:
        return tuple(entry for (pk, _), entry in sorted(self._entries.items()) if pk == clamp_id)

    def clamp(self, clamp_id: str) -> Clamp | None:
        header = self._clamps.get(clamp_id)
        if header is None:
            return None
        return _replace_clamp(header, entries=self.entries_for(clamp_id))

    def active(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self._entries.values() if entry.active)

    def history(self, limit: int = 50) -> tuple[Clamp, ...]:
        ordered = sorted(self._clamps.values(), key=lambda clamp: clamp.created_at, reverse=True)
        return tuple(ordered[:limit])


# --- helpers ------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _replace(entry: Entry, **changes: Any) -> Entry:
    """`dataclasses.replace`, spelled out.

    `replace` on a frozen dataclass holding a `Mapping` field works, but
    being explicit here means the fields an update may change are visible in
    one place — and `status` is the field this module exists to get right.
    """
    current = {
        "clamp_id": entry.clamp_id,
        "target": entry.target,
        "status": entry.status,
        "previous_state": entry.previous_state,
        "applied_state": entry.applied_state,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "dry_run": entry.dry_run,
        "error_code": entry.error_code,
        "error_message": entry.error_message,
        "skip_reason": entry.skip_reason,
    }
    current.update(changes)
    return Entry(**current)  # type: ignore[arg-type]


def _replace_clamp(clamp: Clamp, **changes: Any) -> Clamp:
    current = {
        "clamp_id": clamp.clamp_id,
        "created_at": clamp.created_at,
        "mode": clamp.mode,
        "actor": clamp.actor,
        "limit_id": clamp.limit_id,
        "limit_amount": clamp.limit_amount,
        "spend": clamp.spend,
        "reason": clamp.reason,
        "stack_name": clamp.stack_name,
        "entries": clamp.entries,
    }
    current.update(changes)
    return Clamp(**current)  # type: ignore[arg-type]


def _encode(value: Mapping[str, Any]) -> dict[str, Any]:
    """Python to DynamoDB attribute values.

    `None` is written as a NULL attribute rather than omitted, so a missing
    key and a null key are not two different questions for a reader to ask —
    the same rule the JSON schema follows.
    """
    return {key: _SERIALISER.serialize(_clean(item)) for key, item in value.items()}


def _decode(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _DESERIALISER.deserialize(value) for key, value in item.items()}


def _clean(value: Any) -> Any:
    """Make a value DynamoDB can hold.

    Floats are the only real trap: DynamoDB has no float type, and the
    serialiser refuses one rather than rounding it. Amounts are money, so
    they go in as strings-of-decimal via `Decimal`, which round-trips
    exactly.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, str) and value == "":
        # DynamoDB allows empty strings in non-key attributes now, but an
        # empty string and an absent value mean the same thing in every
        # field here, and `None` reads better in an export.
        return None
    return value
