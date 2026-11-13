"""The write-ahead ledger, against a DynamoDB that holds state.

The tests that matter are the ones about ordering and about failure. A
ledger that records the right thing on the happy path and nothing useful on
a crash is a ledger that cannot release anything when it is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from support import REGION

from clamper.core.actuator import Call, Result
from clamper.core.ledger import (
    STATUS_CLAMPED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RELEASE_FAILED,
    STATUS_RELEASED,
    STATUS_SKIPPED,
    TABLE_ENV,
    Clamp,
    Entry,
    Ledger,
    LedgerError,
    MemoryLedger,
    Target,
    new_clamp_id,
    table_name_from_env,
)

TARGET = Target(
    action_id="ec2-instance",
    target_id="i-0123456789abcdef0",
    region=REGION,
    title="EC2 instance",
    description="m5.24xlarge in us-east-1",
)

PREVIOUS = {"state": "running"}
APPLIED = {"state": "stopped"}


def a_clamp(clamp_id: str, *, mode: str = "enforce") -> Clamp:
    return Clamp(
        clamp_id=clamp_id,
        created_at="2026-11-13T15:30:00+00:00",
        mode=mode,
        actor="enforcer-schedule",
        limit_id="whole-account",
        limit_amount=500.0,
        spend=612.40,
        reason="actual spend is 122% of the limit",
        stack_name="clamper",
    )


def a_call() -> Call:
    return Call(
        action_id=TARGET.action_id,
        phase="clamp",
        params={"InstanceIds": [TARGET.target_id]},
        target_id=TARGET.target_id,
        region=REGION,
    )


def success() -> Result:
    return Result(call=a_call(), ok=True)


def failure(code: str = "IncorrectInstanceState") -> Result:
    return Result(call=a_call(), ok=False, error_code=code, error_message="not stoppable")


def dry() -> Result:
    return Result(call=a_call(), ok=True, dry_run=True)


# --- write-ahead --------------------------------------------------------


def test_record_writes_the_previous_state_as_pending(ledger: Ledger) -> None:
    """The whole design in one test. Before the call happens, the ledger
    already knows what to put back and does not claim anything worked."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)

    assert entry.status == STATUS_PENDING
    assert entry.previous_state == PREVIOUS
    assert entry.clamped is False
    assert entry.outcome_unknown is True

    # And it is really in the table, not just in the returned object.
    stored = ledger.entries_for(clamp_id)
    assert len(stored) == 1
    assert stored[0].status == STATUS_PENDING
    assert stored[0].previous_state == PREVIOUS


def test_a_crash_between_record_and_the_call_leaves_pending(ledger: Ledger) -> None:
    """The failure this ordering exists for.

    Simulated by simply not calling `settle`, which is exactly what a killed
    process does. The entry must be recoverable and must not look like a
    success or a clean run.
    """
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)

    clamp = ledger.clamp(clamp_id)
    assert clamp is not None
    assert len(clamp.unknown) == 1
    assert clamp.clamped == ()
    # The important assertion: it appears under "not clamped", so no report
    # can show this as done.
    assert len(clamp.not_clamped) == 1
    # And what to restore survived the crash.
    assert clamp.unknown[0].previous_state == PREVIOUS


def test_a_pending_entry_is_not_active(ledger: Ledger) -> None:
    """`release --all` must not try to start something that may never have
    been stopped. Pending is settled by the next reconcile from live state."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)
    assert ledger.active() == ()


def test_settle_marks_a_successful_call_clamped(ledger: Ledger) -> None:
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)
    settled = ledger.settle(entry, success())

    assert settled.status == STATUS_CLAMPED
    assert settled.clamped is True
    assert settled.previous_state == PREVIOUS
    assert ledger.entries_for(clamp_id)[0].status == STATUS_CLAMPED


def test_settle_keeps_the_previous_state_intact(ledger: Ledger) -> None:
    """A settle rewrites the whole item, so the thing most likely to be lost
    is the field that matters most."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.record(clamp_id, TARGET, {"min_size": 2, "max_size": 10}, {"min_size": 0})
    settled = ledger.settle(entry, success())
    assert settled.previous_state == {"min_size": 2, "max_size": 10}
    assert ledger.entries_for(clamp_id)[0].previous_state == {"min_size": 2, "max_size": 10}


# --- failure is recorded as failure -------------------------------------


def test_a_failed_call_settles_to_failed_with_the_error(ledger: Ledger) -> None:
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)
    settled = ledger.settle(entry, failure("UnauthorizedOperation"))

    assert settled.status == STATUS_FAILED
    assert settled.clamped is False
    assert settled.error_code == "UnauthorizedOperation"
    assert settled.error_message == "not stoppable"


def test_a_failed_entry_is_counted_as_not_clamped(ledger: Ledger) -> None:
    """The misreport the threat model calls a security issue: somebody reads
    "stopped 14 resources", believes the spend stopped, and goes to bed."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    for index, result in enumerate([success(), failure(), dry()]):
        target = Target(
            action_id="ec2-instance",
            target_id=f"i-{index}",
            region=REGION,
            title="EC2 instance",
        )
        ledger.settle(ledger.record(clamp_id, target, PREVIOUS, APPLIED), result)

    clamp = ledger.clamp(clamp_id)
    assert clamp is not None
    assert len(clamp.clamped) == 1
    assert len(clamp.not_clamped) == 2


def test_a_failed_entry_is_not_active(ledger: Ledger) -> None:
    """Nothing to release: the resource was never stopped."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), failure())
    assert ledger.active() == ()


# --- dry run ------------------------------------------------------------


def test_a_dry_run_settles_to_dry_run_not_clamped(ledger: Ledger) -> None:
    """Different facts. Only one of them stopped any spending."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id, mode="dry-run"))
    settled = ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), dry())

    assert settled.status == STATUS_DRY_RUN
    assert settled.clamped is False
    assert settled.dry_run is True


def test_a_dry_run_entry_is_not_active(ledger: Ledger) -> None:
    """`release --all` after a dry run must find nothing to do. Trying to
    start resources that were never stopped is how a dry run causes an
    incident."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id, mode="dry-run"))
    ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), dry())
    assert ledger.active() == ()


def test_a_dry_run_clamp_header_knows_it_was_one(ledger: Ledger) -> None:
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id, mode="dry-run"))
    clamp = ledger.clamp(clamp_id)
    assert clamp is not None
    assert clamp.dry_run is True


# --- skips --------------------------------------------------------------


def test_a_skip_is_recorded_with_its_reason(ledger: Ledger) -> None:
    """Written down, not only reported, so "nothing was clamped" and "forty
    things were skipped for a reason nobody recorded" stay distinguishable a
    month later."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.skip(clamp_id, TARGET, "carries clamper:protect")

    assert entry.status == STATUS_SKIPPED
    assert entry.skip_reason == "carries clamper:protect"
    assert entry.clamped is False
    assert ledger.entries_for(clamp_id)[0].skip_reason == "carries clamper:protect"


def test_a_skip_is_not_active(ledger: Ledger) -> None:
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.skip(clamp_id, TARGET, "already stopped")
    assert ledger.active() == ()


# --- release ------------------------------------------------------------


def test_a_release_marks_the_entry_released(ledger: Ledger) -> None:
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), success())
    released = ledger.mark_released(entry, success())

    assert released.status == STATUS_RELEASED
    assert released.active is False
    assert ledger.active() == ()


def test_a_failed_release_stays_visible_as_a_failed_release(ledger: Ledger) -> None:
    """Not reverted to `clamped`. "We tried to put this back and could not"
    is a different thing to tell somebody than "this is still stopped"."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), success())
    released = ledger.mark_released(entry, failure("InvalidInstanceID.NotFound"))

    assert released.status == STATUS_RELEASE_FAILED
    assert released.error_code == "InvalidInstanceID.NotFound"


def test_a_dry_run_release_does_not_mark_released(ledger: Ledger) -> None:
    """`mark_released` uses `performed`, not `ok`, so a dry-run release
    leaves the entry needing a real one."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    entry = ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), success())
    released = ledger.mark_released(entry, dry())
    assert released.status == STATUS_RELEASE_FAILED


# --- queries ------------------------------------------------------------


def test_active_finds_clamped_entries_across_clamps(ledger: Ledger) -> None:
    """Via the GSI, not a scan: the ledger is never pruned, so a scan gets
    slower every month and this is the query `status` makes every time."""
    for index in range(3):
        clamp_id = new_clamp_id()
        ledger.open_clamp(a_clamp(clamp_id))
        target = Target(
            action_id="ec2-instance",
            target_id=f"i-{index}",
            region=REGION,
            title="EC2 instance",
        )
        ledger.settle(ledger.record(clamp_id, target, PREVIOUS, APPLIED), success())

    active = ledger.active()
    assert len(active) == 3
    assert {entry.target.target_id for entry in active} == {"i-0", "i-1", "i-2"}


def test_active_does_not_return_clamp_headers(ledger: Ledger) -> None:
    """The header carries a status too, so the GSI returns it. An entry-less
    header reaching `release --all` would be a release of nothing with a
    confusing error."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.settle(ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED), success())
    assert all(entry.target.target_id for entry in ledger.active())
    assert len(ledger.active()) == 1


def test_entries_for_excludes_the_header(ledger: Ledger) -> None:
    """The obvious key condition for this — `sk <> :header` — is invalid in
    DynamoDB, so the filtering is in Python and worth a test."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    ledger.record(clamp_id, TARGET, PREVIOUS, APPLIED)
    entries = ledger.entries_for(clamp_id)
    assert len(entries) == 1
    assert entries[0].target.target_id == TARGET.target_id


def test_clamp_returns_none_for_an_unknown_id(ledger: Ledger) -> None:
    assert ledger.clamp("20261113t000000z-aaaaaa") is None


def test_a_malformed_clamp_id_is_refused(ledger: Ledger) -> None:
    """Rather than queried. A clamp id reaches the ledger from a command line
    and from a notification somebody pasted."""
    with pytest.raises(LedgerError) as excinfo:
        ledger.clamp("../../etc/passwd")
    assert "clamper ledger" in str(excinfo.value)


def test_history_lists_runs_newest_first(ledger: Ledger) -> None:
    ids = []
    for index in range(3):
        clamp_id = new_clamp_id()
        ids.append(clamp_id)
        clamp = Clamp(
            clamp_id=clamp_id,
            created_at=f"2026-11-1{index}T10:00:00+00:00",
            mode="enforce",
            actor="cli",
            limit_id="whole-account",
            limit_amount=500.0,
            spend=600.0,
            reason="over",
        )
        ledger.open_clamp(clamp)
    history = ledger.history()
    assert [clamp.created_at for clamp in history] == [
        "2026-11-12T10:00:00+00:00",
        "2026-11-11T10:00:00+00:00",
        "2026-11-10T10:00:00+00:00",
    ]


def test_history_respects_its_limit(ledger: Ledger) -> None:
    for index in range(5):
        ledger.open_clamp(
            Clamp(
                clamp_id=f"20261113t00000{index}z-aaaaaa",
                created_at=f"2026-11-13T10:00:0{index}+00:00",
                mode="enforce",
                actor="cli",
                limit_id="a",
                limit_amount=1.0,
                spend=2.0,
                reason="over",
            )
        )
    assert len(ledger.history(limit=2)) == 2


# --- data handling ------------------------------------------------------


def test_money_round_trips_exactly(ledger: Ledger) -> None:
    """DynamoDB has no float type and the serialiser refuses one rather than
    rounding, so amounts go in as Decimal. If this breaks, every spend
    figure in every report is wrong or the write throws."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(
        Clamp(
            clamp_id=clamp_id,
            created_at="2026-11-13T15:30:00+00:00",
            mode="enforce",
            actor="cli",
            limit_id="a",
            limit_amount=1234.56,
            spend=1301.07,
            reason="over",
        )
    )
    clamp = ledger.clamp(clamp_id)
    assert clamp is not None
    assert clamp.limit_amount == 1234.56
    assert clamp.spend == 1301.07


def test_nested_previous_state_round_trips(ledger: Ledger) -> None:
    """Auto Scaling records three numbers; a future action might record a
    structure. Losing a nested value means an unrestorable clamp."""
    clamp_id = new_clamp_id()
    ledger.open_clamp(a_clamp(clamp_id))
    previous = {"min_size": 2, "max_size": 10, "desired_capacity": 6, "tags": ["a", "b"]}
    entry = ledger.record(clamp_id, TARGET, previous, {"min_size": 0})
    ledger.settle(entry, success())
    assert ledger.entries_for(clamp_id)[0].previous_state == previous


def test_a_ledger_write_failure_raises_rather_than_being_swallowed(
    clients: Any, dynamodb_table: str
) -> None:
    """A clamp that cannot be recorded must not happen. An unrecorded clamp
    is an unreleasable one, which is the failure this design exists for."""
    ledger = Ledger(clients, "a-table-that-does-not-exist")
    with pytest.raises(LedgerError) as excinfo:
        ledger.record("20261113t150000z-aaaaaa", TARGET, PREVIOUS, APPLIED)
    assert "Nothing has been clamped" in str(excinfo.value)


# --- identifiers --------------------------------------------------------


def test_clamp_ids_sort_chronologically() -> None:
    """Time first, so a lexical sort is a chronological one — which is what
    makes the DynamoDB range query useful."""
    from datetime import UTC, datetime

    early = new_clamp_id(now=datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC))
    late = new_clamp_id(now=datetime(2026, 11, 13, 11, 0, 0, tzinfo=UTC))
    assert early < late


def test_clamp_ids_are_unique_within_a_second() -> None:
    """Two runs in the same second are possible — an SNS notification and a
    scheduled reconcile arriving together — and a collision would merge two
    runs into one set of entries."""
    from datetime import UTC, datetime

    moment = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ids = {new_clamp_id(now=moment) for _ in range(200)}
    assert len(ids) == 200


def test_a_clamp_id_is_safe_to_paste_into_a_shell() -> None:
    """It appears in a `clamper release <id>` command in a notification."""
    import re

    for _ in range(50):
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", new_clamp_id())


def test_the_release_command_is_in_the_clamp() -> None:
    """The person reading a notification at 3am did not write the config and
    needs the shortest path from "something stopped" to "I put it back"."""
    clamp = a_clamp("20261113t153000z-a1b2c3")
    assert clamp.release_command == "clamper release 20261113t153000z-a1b2c3"


# --- the table name -----------------------------------------------------


def test_the_table_name_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TABLE_ENV, "clamper-prod-ledger")
    assert table_name_from_env() == "clamper-prod-ledger"


def test_a_missing_table_name_is_an_error_not_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A function that guessed a table name could write a clamp record
    somewhere nobody would look for it."""
    monkeypatch.delenv(TABLE_ENV, raising=False)
    with pytest.raises(LedgerError) as excinfo:
        table_name_from_env()
    assert "no default" in str(excinfo.value)


# --- the in-memory ledger behaves identically ---------------------------


@pytest.mark.parametrize("make", [MemoryLedger, None], ids=["memory", "dynamodb"])
def test_both_ledgers_agree_on_the_lifecycle(make: Any, ledger: Ledger, fixed_now: str) -> None:
    """`clamper simulate` runs the real enforcer against `MemoryLedger`, so
    a divergence between the two would mean the thing a user tries out is
    not the thing that runs in their account."""
    store: Any = make() if make else ledger
    clamp_id = new_clamp_id()
    store.open_clamp(a_clamp(clamp_id))

    entry = store.record(clamp_id, TARGET, PREVIOUS, APPLIED)
    assert entry.status == STATUS_PENDING

    settled = store.settle(entry, success())
    assert settled.status == STATUS_CLAMPED
    assert len(store.active()) == 1

    released = store.mark_released(settled, success())
    assert released.status == STATUS_RELEASED
    assert store.active() == ()

    clamp = store.clamp(clamp_id)
    assert clamp is not None
    assert len(clamp.entries) == 1


def test_the_memory_ledger_names_itself_as_not_real() -> None:
    """So `simulate` output cannot be mistaken for something that touched an
    account."""
    assert MemoryLedger().table_name == "(in memory)"


def test_an_entry_round_trips_through_its_item_form() -> None:
    """Both halves of the serialisation, without AWS in the way."""
    entry = Entry(
        clamp_id="20261113t153000z-a1b2c3",
        target=TARGET,
        status=STATUS_CLAMPED,
        previous_state=PREVIOUS,
        applied_state=APPLIED,
        created_at="2026-11-13T15:30:00+00:00",
        updated_at="2026-11-13T15:30:05+00:00",
    )
    again = Entry.from_item(entry.to_item())
    assert again == entry
