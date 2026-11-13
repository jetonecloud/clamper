"""`clamper clamp`: the enforcer's run, by hand, against moto."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from support import REGION, spend_reader
from test_resource_ec2 import launch
from typer.testing import CliRunner

from clamper import cli

runner = CliRunner()

MANAGED = {"clamper:managed": "true"}


def write_config(tmp_path: Path, *, mode: str = "enforce", cap: int = 50) -> Path:
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: {mode}\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n"
        f"safety:\n  max_actions_per_run: {cap}\n",
        encoding="utf-8",
    )
    return path


def state_of(ec2: Any, instance_id: str) -> str:
    found = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return str(found[0]["Instances"][0]["State"]["Name"])


def test_under_the_limit_clamp_does_nothing(
    tmp_path: Path,
    cli_aws: Any,
    stack_ledger: Any,
    moto_ec2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(10.0))
    instance = launch(moto_ec2, tags=MANAGED)
    result = runner.invoke(cli.app, ["clamp", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert state_of(moto_ec2, instance) == "running"
    assert stack_ledger.history() == ()


def test_over_the_limit_clamp_stops_and_records(
    tmp_path: Path,
    cli_aws: Any,
    stack_ledger: Any,
    moto_ec2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    instance = launch(moto_ec2, tags=MANAGED)
    result = runner.invoke(cli.app, ["clamp", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 1, result.output
    assert state_of(moto_ec2, instance) in {"stopping", "stopped"}
    assert "clamper release" in result.stdout
    (header,) = stack_ledger.history()
    assert header.actor.startswith("cli:arn:aws:iam::")


def test_in_dry_run_clamp_records_and_stops_nothing(
    tmp_path: Path,
    cli_aws: Any,
    stack_ledger: Any,
    moto_ec2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    instance = launch(moto_ec2, tags=MANAGED)
    result = runner.invoke(
        cli.app, ["clamp", "--config", str(write_config(tmp_path, mode="dry-run"))]
    )
    assert state_of(moto_ec2, instance) == "running"
    assert "WOULD CLAMP (dry-run)" in result.stdout


def test_over_the_cap_nothing_is_clamped_until_yes(
    tmp_path: Path,
    cli_aws: Any,
    stack_ledger: Any,
    moto_ec2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    instances = [launch(moto_ec2, tags=MANAGED) for _ in range(2)]
    path = str(write_config(tmp_path, cap=1))

    held = runner.invoke(cli.app, ["clamp", "--config", path])
    assert held.exit_code == 1
    assert all(state_of(moto_ec2, item) == "running" for item in instances)
    assert "re-run with --yes" in held.stderr

    forced = runner.invoke(cli.app, ["clamp", "--yes", "--config", path])
    assert forced.exit_code == 1, forced.output
    assert all(state_of(moto_ec2, item) in {"stopping", "stopped"} for item in instances)
    assert "overridden by hand" in forced.stdout


def test_yes_does_not_override_protect(
    tmp_path: Path,
    cli_aws: Any,
    stack_ledger: Any,
    moto_ec2: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    protected = launch(moto_ec2, tags={**MANAGED, "clamper:protect": "true"})
    runner.invoke(cli.app, ["clamp", "--yes", "--config", str(write_config(tmp_path))])
    assert state_of(moto_ec2, protected) == "running"
