"""`clamper simulate` and `clamper policy`."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from support import ACCOUNT, REGION, spend_reader
from test_resource_ec2 import launch
from typer.testing import CliRunner

from clamper import cli

runner = CliRunner()

MANAGED = {"clamper:managed": "true"}
IAM = Path(__file__).resolve().parent.parent / "iam"


def write_config(tmp_path: Path, mode: str = "enforce") -> Path:
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: {mode}\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n",
        encoding="utf-8",
    )
    return path


def state_of(ec2: Any, instance_id: str) -> str:
    found = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return str(found[0]["Instances"][0]["State"]["Name"])


def test_simulate_never_acts_even_in_enforce_mode(
    tmp_path: Path, cli_aws: Any, moto_ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ledger table exists here at all: simulate must not need one."""
    monkeypatch.setattr(cli, "read_spend", spend_reader(150.0))
    instance = launch(moto_ec2, tags=MANAGED)
    result = runner.invoke(cli.app, ["simulate", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 1, result.output
    assert state_of(moto_ec2, instance) == "running"
    assert "WOULD CLAMP (dry-run)" in result.stdout
    assert instance in result.stdout
    assert "in-memory ledger" in result.stdout


def test_assume_breach_clamps_on_paper_whatever_the_spend(
    tmp_path: Path, cli_aws: Any, moto_ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(1.0))
    instance = launch(moto_ec2, tags=MANAGED)
    path = str(write_config(tmp_path))

    calm = runner.invoke(cli.app, ["simulate", "--config", path])
    assert calm.exit_code == 0, calm.output
    assert instance not in calm.stdout

    breached = runner.invoke(cli.app, ["simulate", "--assume-breach", "--config", path])
    assert instance in breached.stdout
    assert "--assume-breach" in breached.stdout
    assert state_of(moto_ec2, instance) == "running"


@pytest.mark.parametrize("kind", ["enforcer", "operator"])
def test_policy_from_the_starter_config_is_the_committed_example(tmp_path: Path, kind: str) -> None:
    """So the regeneration hint in test_policy.py works: `clamper init`, then
    `clamper policy`."""
    starter = tmp_path / "clamper.yml"
    starter.write_text(
        resources.files("clamper.data").joinpath("clamper.starter.yml").read_text("utf-8"),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app, ["policy", "--kind", kind, "--account", ACCOUNT, "--config", str(starter)]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == (IAM / f"{kind}-policy.example.json").read_text(encoding="utf-8")
    json.loads(result.stdout)


def test_policy_refuses_a_malformed_account(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["policy", "--account", "12345", "--config", str(write_config(tmp_path))],
    )
    assert result.exit_code == 2
    assert "12-digit" in result.stderr
