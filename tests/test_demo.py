"""`clamper demo`: the real run, against fixtures, with no AWS at all."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from clamper import cli, demo
from clamper.core.report import model

runner = CliRunner()


@pytest.fixture()
def no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to build a real session fails the test."""
    import boto3

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the demo tried to use AWS")

    monkeypatch.setattr(boto3, "Session", refuse)
    monkeypatch.setattr(cli, "_aws", refuse)


def test_the_demo_shows_every_kind_of_line(no_aws: None) -> None:
    shown = demo.run_demo()
    statuses = {line.status for line in (*shown.run.report.acted, *shown.run.report.not_acted)}
    assert {model.CLAMPED, model.FAILED, model.SKIPPED, model.EXCLUDED} <= statuses
    assert shown.run.report.protected
    assert shown.plan.unsupported
    assert shown.run.report.release_command


def test_a_refused_call_is_never_counted_as_done(no_aws: None) -> None:
    shown = demo.run_demo()
    (failed,) = [line for line in shown.run.report.not_acted if line.status == model.FAILED]
    assert failed.target_id == "i-0c3d4e5f6a7b8c9d0"
    assert failed.target_id not in {line.target_id for line in shown.run.report.acted}


def test_the_demo_never_asks_to_stop_clamper_or_a_protected_resource(no_aws: None) -> None:
    shown = demo.run_demo()
    touched = {str(value) for _, _, params in shown.account.calls for value in params.values()}
    assert not any("clamper-enforcer" in value for value in touched)
    assert not any("i-0e5f6a7b8c9d0e1f2" in value for value in touched)


def test_clamper_demo_exits_0_and_prints_both_reports_and_the_email(no_aws: None) -> None:
    result = runner.invoke(cli.app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "clamper plan" in result.stdout
    assert "clamper run" in result.stdout
    assert "The notification that run would send" in result.stdout
    assert "clamper release" in result.stdout


def test_clamper_demo_writes_json_that_parses(no_aws: None, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["demo", "-f", "json", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for kind in ("plan", "run"):
        document = json.loads((tmp_path / f"clamper-{kind}-report.json").read_text())
        assert document["kind"] == kind
