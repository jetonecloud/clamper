"""`clamper release` through the CLI, against moto.

`release()` has its own tests for every outcome. These check the command:
which entries it picks, that it ignores `mode`, that `--dry-run` writes
nothing, and the exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from support import REGION, record_clamp
from test_resource_ec2 import launch
from typer.testing import CliRunner

from clamper import cli
from clamper.core.ledger import STATUS_CLAMPED, STATUS_RELEASED

runner = CliRunner()

CLAMP = "20261113t153000-abcd"


def write_config(tmp_path: Path, mode: str = "enforce") -> Path:
    path = tmp_path / "clamper.yml"
    path.write_text(
        f"version: 1\nmode: {mode}\nregions: [{REGION}]\n"
        "limits:\n  - id: whole-account\n    amount_usd: 100\n",
        encoding="utf-8",
    )
    return path


def stopped(ec2: Any) -> str:
    """An instance in the state a clamp leaves it in."""
    instance_id = launch(ec2)
    ec2.stop_instances(InstanceIds=[instance_id])
    return instance_id


def state_of(ec2: Any, instance_id: str) -> str:
    found = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    return str(found[0]["Instances"][0]["State"]["Name"])


def test_release_starts_what_the_clamp_stopped(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, moto_ec2: Any
) -> None:
    instance_id = stopped(moto_ec2)
    record_clamp(stack_ledger, (instance_id,), clamp_id=CLAMP)

    result = runner.invoke(cli.app, ["release", CLAMP, "--config", str(write_config(tmp_path))])

    assert result.exit_code == 0, result.output
    assert "RELEASED (1)" in result.stdout
    assert state_of(moto_ec2, instance_id) in {"pending", "running"}
    assert [entry.status for entry in stack_ledger.entries_for(CLAMP)] == [STATUS_RELEASED]


def test_release_ignores_a_dry_run_config(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, moto_ec2: Any
) -> None:
    """A clamp made in enforce must stay releasable after the config goes
    back to dry-run."""
    instance_id = stopped(moto_ec2)
    record_clamp(stack_ledger, (instance_id,), clamp_id=CLAMP)
    result = runner.invoke(
        cli.app, ["release", CLAMP, "--config", str(write_config(tmp_path, "dry-run"))]
    )
    assert result.exit_code == 0, result.output
    assert state_of(moto_ec2, instance_id) in {"pending", "running"}


def test_dry_run_release_changes_nothing_anywhere(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, moto_ec2: Any
) -> None:
    instance_id = stopped(moto_ec2)
    record_clamp(stack_ledger, (instance_id,), clamp_id=CLAMP)
    result = runner.invoke(
        cli.app, ["release", CLAMP, "--dry-run", "--config", str(write_config(tmp_path))]
    )
    assert result.exit_code == 0, result.output
    assert "would-release" in result.stdout
    assert "DRY-RUN" in result.stdout
    assert state_of(moto_ec2, instance_id) == "stopped"
    assert [entry.status for entry in stack_ledger.entries_for(CLAMP)] == [STATUS_CLAMPED]


def test_release_all_with_a_target_releases_only_that_one(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, moto_ec2: Any
) -> None:
    first, second = stopped(moto_ec2), stopped(moto_ec2)
    record_clamp(stack_ledger, (first, second), clamp_id=CLAMP)
    result = runner.invoke(
        cli.app,
        ["release", "--all", "--target", first, "--config", str(write_config(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert state_of(moto_ec2, first) in {"pending", "running"}
    assert state_of(moto_ec2, second) == "stopped"


def test_a_hand_made_change_is_a_conflict_and_exit_2(
    tmp_path: Path, cli_aws: Any, stack_ledger: Any, moto_ec2: Any
) -> None:
    instance_id = launch(moto_ec2)  # running: somebody started it by hand
    record_clamp(stack_ledger, (instance_id,), clamp_id=CLAMP)
    result = runner.invoke(
        cli.app,
        ["release", CLAMP, "--format", "json", "--config", str(write_config(tmp_path))],
    )
    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert document["not_acted"][0]["status"] == "conflict"
    assert [entry.status for entry in stack_ledger.entries_for(CLAMP)] == [STATUS_CLAMPED]


def test_nothing_left_to_release_is_exit_0(tmp_path: Path, cli_aws: Any, stack_ledger: Any) -> None:
    result = runner.invoke(cli.app, ["release", "--all", "--config", str(write_config(tmp_path))])
    assert result.exit_code == 0
    assert "Nothing to release" in result.stderr


def test_one_clamp_or_all_but_not_both_or_neither(tmp_path: Path) -> None:
    path = str(write_config(tmp_path))
    neither = runner.invoke(cli.app, ["release", "--config", path])
    both = runner.invoke(cli.app, ["release", CLAMP, "--all", "--config", path])
    assert neither.exit_code == 2 and both.exit_code == 2
    assert "or pass --all" in neither.stderr
