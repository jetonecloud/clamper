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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from clamper import __version__
from clamper.core import config as config_module
from clamper.core import session as session_module
from clamper.core.actions import load_registry
from clamper.core.budgets import Spend, read_spend
from clamper.core.config import Config, ConfigError, Limit
from clamper.core.discover import discover
from clamper.core.plan import build_plan
from clamper.core.report import EXTENSIONS, FORMATS, model, render
from clamper.core.session import Clients, Identity
from clamper.core.targets import SelfIdentity

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


ProfileOption = Annotated[str | None, typer.Option("--profile", help="AWS profile to use.")]
RegionOption = Annotated[
    str | None, typer.Option("--region", help="AWS region for the stack and the CLI's calls.")
]
RoleOption = Annotated[
    str | None, typer.Option("--role-arn", help="Role to assume from your current credentials.")
]
FormatOption = Annotated[
    list[str] | None,
    typer.Option(
        "--format", "-f", help=f"Output format, repeatable. One of: {', '.join(FORMATS)}."
    ),
]
OutputOption = Annotated[
    Path | None, typer.Option("--output-dir", "-o", help="Write reports here instead of stdout.")
]


@dataclass(frozen=True)
class Aws:
    """Credentials resolved once per command."""

    session: Any
    clients: Clients
    identity: Identity

    @property
    def actor(self) -> str:
        return f"cli:{self.identity.arn}"


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


def _aws(profile: str | None, region: str | None, role_arn: str | None) -> Aws:
    """A session from the standard chain. Never an access key: there is no
    flag for one, and nothing is written to disk."""
    try:
        session = session_module.build_session(profile=profile, region=region, role_arn=role_arn)
        identity = session_module.whoami(session)
    except session_module.CredentialsError as exc:
        raise _fail(f"{exc} Run `clamper doctor` to see what is wrong.") from exc
    if identity.is_root:
        _err(
            "warning: these are the account root user's credentials. Use a role with the "
            "operator policy in iam/ instead."
        )
    return Aws(session=session, clients=Clients(session), identity=identity)


def _regions(config: Config, aws: Aws) -> tuple[str, ...]:
    if config.regions:
        return config.regions
    if aws.clients.default_region:
        return (aws.clients.default_region,)
    raise _fail("no region: set `regions:` in clamper.yml or pass --region.")


def _formats(formats: Sequence[str] | None) -> list[str]:
    chosen = list(formats or ["terminal"])
    for item in chosen:
        if item not in FORMATS:
            raise typer.BadParameter(
                f"{item!r} is not a format. One of: {', '.join(FORMATS)}.", param_hint="--format"
            )
    return chosen


def _emit(report: model.Report, formats: Sequence[str], output_dir: Path | None) -> None:
    """Print, or write one file per format.

    Stable filenames, overwritten each run, which is what a CI artifact
    wants. `.gitignore` covers `clamper-plan-*` and its siblings, because a
    plan names your resources.
    """
    if output_dir is None:
        for fmt in formats:
            click.echo(render(report, fmt), nl=False)
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for fmt in formats:
        path = output_dir / f"clamper-{report.kind}-report.{EXTENSIONS[fmt]}"
        path.write_text(render(report, fmt), encoding="utf-8")
        _err(f"wrote {path}")


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


@app.command()
def plan(
    config: ConfigOption = None,
    formats: FormatOption = None,
    output_dir: OutputOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Show what would be clamped if a limit were breached now. Read-only.

    Treats every limit as breached, because the question is "what have I
    told Clamper it may stop?". Lists what is protected, what was opted in
    and will not be clamped and why, and what Clamper found and will never
    touch. Never builds anything that can write.
    """
    loaded = _load_config(config)
    chosen = _formats(formats)
    aws = _aws(profile, region, role_arn)
    regions = _regions(loaded, aws)
    registry = load_registry()
    report = build_plan(
        loaded,
        read_spend=_spend_reader(aws, loaded),
        find=lambda: discover(
            aws.clients,
            registry.enabled(loaded.targets.types),
            regions,
            include_unsupported=True,
        ),
        identity=_self_identity(aws, loaded),
        actor=aws.actor,
        account=aws.identity.account,
    )
    _emit(report, chosen, output_dir)
    raise typer.Exit(report.exit_code)


def _spend_reader(aws: Aws, config: Config) -> Callable[[Limit], Spend]:
    def read(limit: Limit) -> Spend:
        return read_spend(aws.clients, aws.identity.account, limit, config.stack_name)

    return read


def _self_identity(aws: Aws, config: Config) -> SelfIdentity:
    """The stack's own resources, with a warning if the list was unreadable.

    The warning goes to stderr rather than into the report: two of the three
    self-exclusion checks still hold, and the report is still true.
    """
    identity = SelfIdentity.from_stack(aws.clients, config.stack_name)
    if identity.read_error:
        _err(
            f"warning: could not read stack {config.stack_name}'s resource list "
            f"({identity.read_error}). Clamper's own resources are still recognised by "
            f"the stack tag and by name."
        )
    return identity


def main() -> None:
    app()
