"""The command line, through Typer's runner. No AWS unless a test says so."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from clamper import __version__
from clamper.cli import app
from clamper.core import config as config_module

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_init_writes_a_valid_dry_run_config(tmp_path: Path) -> None:
    target = tmp_path / "clamper.yml"
    result = runner.invoke(app, ["init", "--path", str(target), "--email", "ops@example.com"])
    assert result.exit_code == 0, result.output
    loaded = config_module.load(target)
    assert loaded.mode == "dry-run"
    assert loaded.notifications.email == "ops@example.com"
    # Every default is written explicitly.
    text = target.read_text(encoding="utf-8")
    assert "mode: dry-run" in text
    assert "max_actions_per_run: 50" in text


def test_init_warns_when_no_one_will_be_told(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--path", str(tmp_path / "clamper.yml")])
    assert result.exit_code == 0
    assert "nobody will hear" in result.stdout


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "clamper.yml"
    target.write_text("keep me", encoding="utf-8")
    result = runner.invoke(app, ["init", "--path", str(target)])
    assert result.exit_code == 2
    assert target.read_text(encoding="utf-8") == "keep me"


def test_validate_reports_a_bad_key_with_its_fix(tmp_path: Path) -> None:
    target = tmp_path / "clamper.yml"
    target.write_text(
        "version: 1\nlimits:\n  - id: a\n    amount_usd: 5\ntargets:\n  protect_tags: x\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", "--config", str(target)])
    assert result.exit_code == 2
    assert "protect_tag" in result.stderr


def test_a_missing_config_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 2
    assert "clamper init" in result.stderr
