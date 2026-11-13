"""The command line.

This module owns all the I/O. Everything under `core/` returns values; the
CLI is the only place that prints, prompts, writes a file or chooses an exit
code. That split is what makes the rest of the package testable without a
filesystem, and it means there is one place to check that nothing sensitive
is written anywhere.

Commands, grouped by what they can do to an account:

- **Change nothing anywhere:** `version`, `init`, `validate`, `demo`,
  `policy`, `deploy --template-only`.
- **Read only:** `plan`, `status`, `ledger`, `doctor`, `simulate`.
- **Write to Clamper's own stack:** `deploy`.
- **Write to your resources, through the actuator:** `clamp`, `release`.

Exit codes, as in Crosswind:

- **0** nothing to do
- **1** a limit is breached, or something is clamped
- **2** something could not be checked or done, or the tool failed

2 beats 1.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Annotated

import click
import typer

from clamper import __version__
from clamper.core import config as config_module
from clamper.core.config import Config, ConfigError

app = typer.Typer(
    name="clamper",
    help=(
        "Hard spending limits for AWS. When a budget is breached, Clamper stops what "
        "you allowed it to stop, records what it was before, and puts it back with "
        "one command."
    ),
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

EXIT_TOOL_FAILED = 2

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Path to clamper.yml. Default: the nearest one at or above this directory.",
    ),
]


def _err(message: str) -> None:
    """Everything that is not the report goes to stderr, so
    `clamper plan --format json > plan.json` works."""
    click.echo(message, err=True)


def _fail(message: str) -> typer.Exit:
    _err(f"error: {message}")
    return typer.Exit(EXIT_TOOL_FAILED)


def _load_config(path: Path | None) -> Config:
    """The config, or a clear exit 2.

    No implied configuration: a tool that can stop infrastructure does not
    run on defaults nobody wrote down.
    """
    found = path or config_module.find_config(Path.cwd())
    if found is None:
        raise _fail(
            "no clamper.yml found here or in any parent directory. "
            "`clamper init` writes a starting point."
        )
    try:
        return config_module.load(found)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def version() -> None:
    """Print the version and exit."""
    click.echo(f"clamper {__version__}")


@app.command()
def init(
    path: Annotated[Path, typer.Option("--path", "-p", help="Where to write the file.")] = Path(
        config_module.CONFIG_FILENAME
    ),
    email: Annotated[
        str | None, typer.Option("--email", help="Where notifications should go.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starting clamper.yml, in dry-run mode.

    The file states every default explicitly, with a comment. A default that
    is absent from the file is a default nobody reads.
    """
    if path.exists() and not force:
        raise _fail(f"{path} already exists. Use --force to overwrite it.")
    text = resources.files("clamper.data").joinpath("clamper.starter.yml").read_text("utf-8")
    if email:
        text = text.replace("  # email: you@example.com", f"  email: {email}", 1)
    try:
        config_module.loads(text, source=str(path))
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    path.write_text(text, encoding="utf-8")
    click.echo(f"Wrote {path}, in dry-run mode.")
    click.echo("")
    click.echo("Next:")
    click.echo("  1. Tag what Clamper may stop:   clamper:managed = true")
    click.echo("  2. Tag what it must never stop: clamper:protect = true")
    click.echo("  3. See what that means:         clamper plan")
    if not email:
        click.echo("")
        click.echo("No notification email is set. Add one under `notifications:` before deploying,")
        click.echo("or nobody will hear about a clamp.")


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Check clamper.yml without touching AWS."""
    loaded = _load_config(config)
    click.echo(f"{loaded.source}: valid.")
    click.echo(f"  mode:   {loaded.mode}")
    click.echo(f"  stack:  {loaded.stack_name}")
    click.echo(
        f"  limits: {', '.join(f'{item.id} ${item.amount_usd:,.2f}' for item in loaded.limits)}"
    )
    click.echo(f"  types:  {', '.join(loaded.targets.types)}")
    click.echo(f"  cap:    {loaded.safety.max_actions_per_run} actions per run")
    if loaded.targets.include_untagged:
        click.echo("  include_untagged is ON: resources nobody tagged are candidates.")
    if not loaded.notifications.email and not loaded.notifications.slack_webhook_parameter:
        click.echo("  warning: no notification is configured. Nobody will hear about a clamp.")


def main() -> None:
    app()
