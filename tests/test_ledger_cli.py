"""`clamper ledger`: the history list, and one clamp in full."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support import REGION, record_clamp
from typer.testing import CliRunner

from clamper import cli
from clamper.core.ledger import MemoryLedger
from clamper.core.report import history

runner = CliRunner()

CLAMP = "20261113t153000-abcd"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: enforce\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n",
        encoding="utf-8",
    )
    return path


def test_an_empty_history_says_so() -> None:
    assert "no clamp runs yet" in history.history_text((), "clamper-ledger")


def test_one_clamp_shows_before_and_after_and_how_to_undo() -> None:
    ledger = MemoryLedger()
    record_clamp(ledger)
    clamp = ledger.clamp(CLAMP)
    assert clamp is not None
    text = history.clamp_text(clamp)
    assert "before:  state=running" in text
    assert "clamped: state=stopped" in text
    assert f"clamper release {CLAMP}" in text
    assert "1 entry:" in text


def test_free_text_from_the_ledger_cannot_reach_the_terminal_raw() -> None:
    ledger = MemoryLedger()
    record_clamp(ledger)
    clamp = ledger.clamp(CLAMP)
    assert clamp is not None
    from dataclasses import replace

    hostile = replace(clamp, actor="ops\x1b[2J\x07", reason="over \x1b]0;pwned\x07")
    text = history.clamp_text(hostile)
    assert "\x1b" not in text and "\x07" not in text


def test_the_json_view_parses_and_carries_the_states() -> None:
    ledger = MemoryLedger()
    record_clamp(ledger)
    clamp = ledger.clamp(CLAMP)
    assert clamp is not None
    document = json.loads(history.clamp_json(clamp))
    entry = document["clamp"]["entries"][0]
    assert entry["previous_state"] == {"state": "running"}
    assert entry["status"] == "clamped"


def test_ledger_lists_runs_through_the_cli(tmp_path: Path, cli_aws: Any, stack_ledger: Any) -> None:
    record_clamp(stack_ledger)
    result = runner.invoke(cli.app, ["ledger", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert CLAMP in result.stdout


def test_ledger_shows_one_clamp_and_exits_1_while_it_is_active(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any
) -> None:
    record_clamp(stack_ledger)
    result = runner.invoke(cli.app, ["ledger", CLAMP, "--config", str(write_config(tmp_path))])
    assert result.exit_code == 1, result.output
    assert "i-0123456789abcdef0" in result.stdout


def test_a_pending_entry_makes_the_clamp_view_exit_2(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any
) -> None:
    record_clamp(stack_ledger, settle=False)
    result = runner.invoke(cli.app, ["ledger", CLAMP, "--config", str(write_config(tmp_path))])
    assert result.exit_code == 2
    assert "[pending]" in result.stdout


def test_an_unknown_clamp_is_exit_2(tmp_path: Path, cli_aws: Any, stack_ledger: Any) -> None:
    result = runner.invoke(
        cli.app, ["ledger", "20200101t000000-ffff", "--config", str(write_config(tmp_path))]
    )
    assert result.exit_code == 2
    assert "no clamp" in result.stderr


def test_a_malformed_clamp_id_never_reaches_aws(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["ledger", "$(rm -rf ~)", "--config", str(write_config(tmp_path))]
    )
    assert result.exit_code == 2
    assert "is not a clamp id" in result.stderr
