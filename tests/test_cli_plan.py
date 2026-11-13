"""`clamper plan` through the CLI, against moto.

The plan's logic has its own tests through `build_plan`. These check the
command: that it reads the config, discovers in the configured regions,
prints to stdout, keeps everything else on stderr, and exits with the
report's code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import REGION, spend_reader
from test_resource_ec2 import launch
from typer.testing import CliRunner

from clamper import cli

runner = CliRunner()

MANAGED = {"clamper:managed": "true"}


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: enforce\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n" + extra,
        encoding="utf-8",
    )
    return path


def test_plan_lists_what_would_be_clamped(
    tmp_path: Path, cli_aws: Any, moto_ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(40.0))
    managed = launch(moto_ec2, tags=MANAGED)
    protected = launch(moto_ec2, tags={**MANAGED, "clamper:protect": "true"})
    untagged = launch(moto_ec2)

    result = runner.invoke(cli.app, ["plan", "--config", str(write_config(tmp_path))])

    # Nothing is breached and nothing is clamped: a plan is exit 0.
    assert result.exit_code == 0, result.output
    assert "WILL CLAMP" in result.stdout
    assert managed in result.stdout
    assert "PROTECTED" in result.stdout and protected in result.stdout
    assert untagged not in result.stdout
    assert "NOT OPTED IN: 1 resource" in result.stdout


def test_plan_writes_json_to_stdout_and_nothing_else(
    tmp_path: Path, cli_aws: Any, moto_ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(40.0))
    launch(moto_ec2, tags=MANAGED)
    result = runner.invoke(
        cli.app, ["plan", "--config", str(write_config(tmp_path)), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["kind"] == "plan"


def test_plan_writes_one_file_per_format(
    tmp_path: Path, cli_aws: Any, moto_ec2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(40.0))
    out = tmp_path / "reports"
    result = runner.invoke(
        cli.app,
        [
            "plan",
            "--config",
            str(write_config(tmp_path)),
            "-f",
            "json",
            "-f",
            "markdown",
            "--output-dir",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert (out / "clamper-plan-report.json").is_file()
    assert (out / "clamper-plan-report.md").is_file()
    assert "wrote" in result.stderr


def test_an_unreadable_budget_makes_the_plan_exit_2(
    tmp_path: Path, cli_aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_spend", spend_reader(None))
    result = runner.invoke(cli.app, ["plan", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 2
    assert "AccessDenied" in result.stdout


def test_an_unknown_format_is_refused_before_any_aws_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_aws(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("plan reached AWS with a bad --format")

    monkeypatch.setattr(cli, "_aws", no_aws)
    result = runner.invoke(
        cli.app, ["plan", "--config", str(write_config(tmp_path)), "--format", "html"]
    )
    assert result.exit_code == 2
    assert "html" in result.stderr
