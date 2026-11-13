"""`clamper status`: the limits, and what the ledger says is stopped."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from support import REGION, record_clamp, spend_reader
from typer.testing import CliRunner

from clamper import cli
from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit, loads
from clamper.core.ledger import LedgerError, MemoryLedger
from clamper.core.report import model
from clamper.core.status import build_status

runner = CliRunner()


def config(mode: str = "enforce") -> Config:
    return loads(
        f"version: 1\nmode: {mode}\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n"
    )


def spend(amount: float) -> Any:
    def read(limit: Limit) -> Spend:
        return Spend(limit=limit, budget_name="b", actual=amount)

    return read


def test_nothing_clamped_and_under_the_limit_is_exit_0() -> None:
    report = build_status(config(), read_spend=spend(10.0), ledger=MemoryLedger())
    assert report.kind == model.KIND_STATUS
    assert report.active == ()
    assert report.exit_code == 0


def test_an_active_clamp_is_listed_with_its_release_command() -> None:
    ledger = MemoryLedger()
    record_clamp(ledger, ("i-0aaaaaaaaaaaaaaaa", "i-0bbbbbbbbbbbbbbbb"))
    report = build_status(config(), read_spend=spend(150.0), ledger=ledger)
    assert {line.target_id for line in report.active} == {
        "i-0aaaaaaaaaaaaaaaa",
        "i-0bbbbbbbbbbbbbbbb",
    }
    assert all(line.clamp_id == "20261113t153000-abcd" for line in report.active)
    assert any("clamper release 20261113t153000-abcd" in note for note in report.notes)
    assert report.exit_code == 1


def test_a_pending_entry_is_outcome_unknown_and_exit_2() -> None:
    ledger = MemoryLedger()
    record_clamp(ledger, settle=False)
    report = build_status(config(), read_spend=spend(150.0), ledger=ledger)
    assert [line.status for line in report.not_acted] == [model.PENDING]
    assert "outcome unknown" in report.not_acted[0].detail
    assert report.exit_code == 2


def test_an_unreadable_ledger_is_never_an_empty_one() -> None:
    class Broken(MemoryLedger):
        def active(self) -> Any:
            raise LedgerError("Could not read the ledger table clamper-ledger: denied")

    report = build_status(config(), read_spend=spend(10.0), ledger=Broken())
    assert report.active == ()
    assert any("ledger" in error for error in report.errors)
    assert report.exit_code == 2


def test_status_through_the_cli(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    record_clamp(stack_ledger)
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: enforce\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["status", "--config", str(path)])
    assert result.exit_code == 1, result.output
    assert "CURRENTLY CLAMPED (1)" in result.stdout
    assert "i-0123456789abcdef0" in result.stdout
    assert "clamp 20261113t153000-abcd" in result.stdout
