"""The files in examples/ are copied by people, so they have to be right."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from clamper.core import config as config_module

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_the_example_config_loads_in_dry_run() -> None:
    loaded = config_module.load(EXAMPLES / "clamper.yml")
    assert loaded.mode == "dry-run"
    assert loaded.notifications.email


def test_the_example_workflow_runs_plan_and_nothing_that_writes() -> None:
    """A workflow a pull request can trigger must never deploy, clamp or
    release. This is the threat model's advice, held as a test."""
    path = EXAMPLES / "workflows" / "clamper-plan.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    commands: list[str] = []
    for job in document["jobs"].values():
        for step in job["steps"]:
            commands += re.findall(r"\bclamper\s+([a-z-]+)", step.get("run", ""))
    assert commands == ["plan"]
