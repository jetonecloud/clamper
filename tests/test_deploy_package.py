"""The enforcer zip and `--template-only`: what ships, and what does not."""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml

from clamper import deploy
from clamper.core.config import loads

CONFIG_TEXT = "version: 1\nmode: dry-run\nlimits:\n  - id: whole-account\n    amount_usd: 100\n"


def names(data: bytes) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_the_zip_carries_the_enforcer_and_its_config() -> None:
    found = names(deploy.build_zip(CONFIG_TEXT))
    assert "clamper/enforcer/handler.py" in found
    assert "clamper/core/actuator.py" in found
    assert "clamper/data/actions.yml" in found
    assert "clamper/data/prices.yml" in found
    assert "yaml/__init__.py" in found
    assert "clamper.yml" in found


def test_the_zip_leaves_out_everything_the_lambda_has_no_use_for() -> None:
    found = names(deploy.build_zip(CONFIG_TEXT))
    for absent in ("clamper/cli.py", "clamper/demo.py", "clamper/deploy.py"):
        assert absent not in found
    assert not any(name.startswith("clamper/data/demo/") for name in found)
    assert not any("__pycache__" in name for name in found)
    # Built for this machine, not for the Lambda: PyYAML falls back without it.
    assert not any(name.endswith((".so", ".pyd")) for name in found)
    assert not any(name.startswith(("typer/", "click/", "rich/", "boto3/")) for name in found)


def test_the_packed_config_is_byte_for_byte_the_users() -> None:
    archive = zipfile.ZipFile(io.BytesIO(deploy.build_zip(CONFIG_TEXT)))
    assert archive.read("clamper.yml").decode("utf-8") == CONFIG_TEXT


def test_the_same_inputs_make_the_same_key() -> None:
    """Content-addressed: no clock in the zip, so no redeploy for nothing."""
    config = loads(CONFIG_TEXT)
    first = deploy.build_artifacts(config, CONFIG_TEXT)
    second = deploy.build_artifacts(config, CONFIG_TEXT)
    assert first.zip_bytes == second.zip_bytes
    assert first.key == second.key
    changed = deploy.build_artifacts(config, CONFIG_TEXT + "# a comment\n")
    assert changed.key != first.key


def test_the_handler_imports_from_the_zip_alone(tmp_path: Path) -> None:
    """Unzip, then import the handler in a clean interpreter that can see
    only the zip and the standard library plus boto3 — no typer, click or
    rich — the way the Lambda runtime will."""
    zipfile.ZipFile(io.BytesIO(deploy.build_zip(CONFIG_TEXT))).extractall(tmp_path)
    script = (
        "import sys\n"
        "for name in ('typer', 'click', 'rich'):\n"
        "    sys.modules[name] = None\n"
        "import clamper.enforcer.handler as h\n"
        "import yaml\n"
        "import clamper\n"
        "print(clamper.__file__)\n"
        "print(yaml.__file__)\n"
        "print(h.config_path())\n"
    )
    result = subprocess.run(  # noqa: S603 - the test's own interpreter
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path), "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    package, pyyaml, config_path = result.stdout.split()
    # Imported from the unzipped tree, not from the developer's environment.
    assert Path(package).is_relative_to(tmp_path)
    assert Path(pyyaml).is_relative_to(tmp_path)
    assert Path(config_path) == tmp_path / "clamper.yml"


def test_template_only_writes_three_files(tmp_path: Path) -> None:
    config = loads(CONFIG_TEXT)
    artifacts = deploy.build_artifacts(config, CONFIG_TEXT)
    written = deploy.write_artifacts(artifacts, tmp_path / "out")
    assert [path.name for path in written] == ["bootstrap.yaml", "main.yaml", artifacts.zip_name]
    for template in written[:2]:
        document = yaml.safe_load(template.read_text(encoding="utf-8"))
        assert document["AWSTemplateFormatVersion"] == "2010-09-09"
    assert written[2].read_bytes() == artifacts.zip_bytes
