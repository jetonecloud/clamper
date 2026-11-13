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
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from clamper import __version__
from clamper import deploy as deploy_module
from clamper.core import config as config_module
from clamper.core import doctor as doctor_module
from clamper.core import session as session_module
from clamper.core.actions import load_registry
from clamper.core.actuator import Actuator
from clamper.core.budgets import Spend, read_spend
from clamper.core.config import Config, ConfigError, Limit
from clamper.core.discover import discover
from clamper.core.ledger import CLAMP_ID, Ledger, LedgerError, MemoryLedger
from clamper.core.names import StackNames
from clamper.core.plan import build_plan
from clamper.core.policy import PolicyError, enforcer_policy, operator_policy
from clamper.core.policy import render as render_policy
from clamper.core.release import release, release_report
from clamper.core.report import EXTENSIONS, FORMATS, history, model, render
from clamper.core.resources import handler_for
from clamper.core.session import Clients, Identity
from clamper.core.status import build_status
from clamper.core.targets import SelfIdentity
from clamper.enforcer.run import TRIGGER_MANUAL, reconcile

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


@app.command()
def status(
    config: ConfigOption = None,
    formats: FormatOption = None,
    output_dir: OutputOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """How close each limit is, and what Clamper has stopped right now. Read-only.

    Exit 1 if anything is clamped, 2 if a clamp's outcome is unknown or
    something could not be read.
    """
    loaded = _load_config(config)
    chosen = _formats(formats)
    aws = _aws(profile, region, role_arn)
    report = build_status(
        loaded,
        read_spend=_spend_reader(aws, loaded),
        ledger=_ledger(aws, loaded),
        actor=aws.actor,
        account=aws.identity.account,
    )
    _emit(report, chosen, output_dir)
    raise typer.Exit(report.exit_code)


LedgerFormat = Annotated[
    str, typer.Option("--format", "-f", help="terminal or json.", show_default=True)
]


@app.command("ledger")
def ledger_command(
    clamp_id: Annotated[
        str | None, typer.Argument(help="Show this clamp run. Omit to list the runs.")
    ] = None,
    config: ConfigOption = None,
    fmt: LedgerFormat = "terminal",
    limit: Annotated[int, typer.Option("--limit", help="How many runs to list.", min=1)] = 20,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """The record of every clamp run, or every entry of one. Read-only.

    Listing exits 0. Showing one run exits 1 if anything in it is still
    clamped and 2 if an entry's outcome is unknown.
    """
    if fmt not in {"terminal", "json"}:
        raise typer.BadParameter(f"{fmt!r} is not terminal or json.", param_hint="--format")
    if clamp_id is not None and not CLAMP_ID.match(clamp_id):
        raise _fail(
            f"{clamp_id!r} is not a clamp id. They look like 20261113t153000z-a1b2c3; "
            f"`clamper ledger` lists them."
        )
    loaded = _load_config(config)
    aws = _aws(profile, region, role_arn)
    store = _ledger(aws, loaded)
    try:
        if clamp_id is None:
            clamps = store.history(limit=limit)
            click.echo(
                history.history_json(clamps)
                if fmt == "json"
                else history.history_text(clamps, store.table_name),
                nl=False,
            )
            raise typer.Exit(0)
        found = store.clamp(clamp_id)
    except LedgerError as exc:
        raise _fail(str(exc)) from exc
    if found is None:
        raise _fail(f"no clamp {clamp_id} in {store.table_name}. `clamper ledger` lists them.")
    click.echo(history.clamp_json(found) if fmt == "json" else history.clamp_text(found), nl=False)
    if found.unknown:
        raise typer.Exit(EXIT_TOOL_FAILED)
    raise typer.Exit(1 if found.active else 0)


@app.command("release")
def release_command(
    clamp_id: Annotated[
        str | None, typer.Argument(help="The clamp run to undo, from the notification.")
    ] = None,
    release_all: Annotated[
        bool, typer.Option("--all", help="Release everything currently clamped.")
    ] = False,
    target: Annotated[str | None, typer.Option("--target", help="Only this resource id.")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Restore even where somebody changed it by hand since."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Say what would be restored; change nothing.")
    ] = False,
    config: ConfigOption = None,
    formats: FormatOption = None,
    output_dir: OutputOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Put back what a clamp stopped, exactly as the ledger recorded it.

    Ignores `mode`: releasing is a person undoing a clamp on purpose, and a
    config in dry-run must never leave a clamp made in enforce unreleasable.
    It has its own --dry-run instead. Anything changed by hand since the
    clamp is a conflict and is left alone unless --force.
    """
    if (clamp_id is None) == (not release_all):
        raise _fail("name one clamp id, or pass --all. Not both, and not neither.")
    if clamp_id is not None and not CLAMP_ID.match(clamp_id):
        raise _fail(f"{clamp_id!r} is not a clamp id. `clamper ledger` lists them.")
    loaded = _load_config(config)
    chosen = _formats(formats)
    aws = _aws(profile, region, role_arn)
    store = _ledger(aws, loaded)
    try:
        entries = store.active() if clamp_id is None else store.entries_for(clamp_id)
    except LedgerError as exc:
        raise _fail(str(exc)) from exc
    entries = tuple(entry for entry in entries if entry.active)
    if target is not None:
        entries = tuple(entry for entry in entries if entry.target.target_id == target)
    if not entries:
        which = f"clamp {clamp_id}" if clamp_id else "the ledger"
        suffix = f" for {target}" if target else ""
        _err(f"nothing in {which} is still clamped{suffix}. Nothing to release.")
        raise typer.Exit(0)

    outcomes = release(
        entries,
        clients=aws.clients,
        ledger=store,
        actuator=Actuator(aws.clients, dry_run=dry_run),
        force=force,
    )
    report = release_report(
        outcomes,
        stack_name=loaded.stack_name,
        dry_run=dry_run,
        actor=aws.actor,
        account=aws.identity.account,
    )
    _emit(report, chosen, output_dir)
    raise typer.Exit(report.exit_code)


@app.command("clamp")
def clamp_command(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Clamp even if the targets are over safety.max_actions_per_run.",
        ),
    ] = False,
    config: ConfigOption = None,
    formats: FormatOption = None,
    output_dir: OutputOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Run the enforcer's reconcile now, from here, as you.

    The same code the Lambda runs: settle anything pending, re-stop what AWS
    restarted, read every budget, and clamp if a limit is breached. It acts
    only on a real breach, and only in `mode: enforce`; in dry-run it records
    what it would have done. --yes overrides the cap and nothing else.
    """
    loaded = _load_config(config)
    chosen = _formats(formats)
    aws = _aws(profile, region, role_arn)
    regions = _regions(loaded, aws)
    registry = load_registry()
    clients = aws.clients
    try:
        outcome = reconcile(
            loaded,
            actuator=Actuator(clients, dry_run=loaded.dry_run, registry=registry),
            ledger=_ledger(aws, loaded),
            identity=_self_identity(aws, loaded),
            read_spend=_spend_reader(aws, loaded),
            find=lambda actions: discover(clients, actions, regions),
            live_state=lambda entry: handler_for(registry.get(entry.target.action_id)).live_state(
                clients, entry
            ),
            actor=aws.actor,
            trigger=TRIGGER_MANUAL,
            account=aws.identity.account,
            registry=registry,
            override_cap=yes,
        )
    except LedgerError as exc:
        raise _fail(str(exc)) from exc
    report = outcome.report
    _emit(report, chosen, output_dir)
    if report.over_cap:
        _err(
            f"Nothing was clamped: {len(report.not_acted)} targets is over the cap of "
            f"{report.cap}. Read the list, then re-run with --yes if it is right."
        )
    if outcome.notify:
        _err(
            "No notification was sent: a clamp run by hand reports here. The ledger has the "
            "record, and `clamper status` shows it."
        )
    raise typer.Exit(report.exit_code)


@app.command()
def simulate(
    assume_breach: Annotated[
        bool,
        typer.Option(
            "--assume-breach",
            help="Treat every limit as breached, whatever the real spend is.",
        ),
    ] = False,
    config: ConfigOption = None,
    formats: FormatOption = None,
    output_dir: OutputOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Run the enforcer's own reconcile against your account, changing nothing.

    Real reads, dry-run forced whatever `mode` says, and an in-memory ledger
    so not even the ledger table is written. What you try out is the code
    that will run in the Lambda, not a separate demo path.
    """
    loaded = _load_config(config)
    chosen = _formats(formats)
    aws = _aws(profile, region, role_arn)
    regions = _regions(loaded, aws)
    registry = load_registry()
    clients = aws.clients
    real = _spend_reader(aws, loaded)

    def spend(limit: Limit) -> Spend:
        found = real(limit)
        if not assume_breach:
            return found
        # Exactly at the clamp threshold, so the decision is a breach and
        # the report's note says the real spend did not decide it.
        return replace(found, actual=limit.clamp_amount, error=None)

    outcome = reconcile(
        loaded,
        actuator=Actuator(clients, dry_run=True, registry=registry),
        ledger=MemoryLedger(),
        identity=_self_identity(aws, loaded),
        read_spend=spend,
        find=lambda actions: discover(clients, actions, regions),
        live_state=lambda entry: handler_for(registry.get(entry.target.action_id)).live_state(
            clients, entry
        ),
        actor=f"simulate:{aws.identity.arn}",
        trigger=TRIGGER_MANUAL,
        account=aws.identity.account,
        registry=registry,
    )
    report = outcome.report
    notes: tuple[str, ...] = (
        "simulate: dry-run forced and an in-memory ledger. Nothing in your account was "
        "changed, the ledger table included.",
    )
    if assume_breach:
        notes += (
            "--assume-breach: every limit was treated as exactly at its clamp threshold. "
            "The real spend did not decide this run.",
        )
    _emit(replace(report, notes=notes + report.notes), chosen, output_dir)
    raise typer.Exit(report.exit_code)


PolicyKind = Annotated[str, typer.Option("--kind", help="enforcer or operator.")]


@app.command()
def policy(
    kind: PolicyKind = "enforcer",
    account: Annotated[
        str, typer.Option("--account", help="The 12-digit account id to scope ARNs to.")
    ] = "",
    policy_region: Annotated[
        str, typer.Option("--region", help="Scope regional ARNs to this region.")
    ] = "*",
    config: ConfigOption = None,
) -> None:
    """Print the IAM policy generated from clamper.yml and actions.yml. No AWS calls.

    `enforcer` is what the Lambda runs as. `operator` is for a person running
    plan, status, ledger, release, clamp and doctor.
    """
    if kind not in {"enforcer", "operator"}:
        raise typer.BadParameter(f"{kind!r} is not enforcer or operator.", param_hint="--kind")
    if not (len(account) == 12 and account.isdigit()):
        raise _fail("--account must be the 12-digit account id the policy is for.")
    loaded = _load_config(config)
    build = enforcer_policy if kind == "enforcer" else operator_policy
    try:
        document = build(loaded, account, region=policy_region)
    except PolicyError as exc:
        raise _fail(str(exc)) from exc
    click.echo(render_policy(document), nl=False)


@app.command()
def doctor(
    config: ConfigOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Check that a clamp would work and somebody would hear about it. Read-only.

    Credentials, the stack, the email subscription, cost allocation tags,
    and every budget. Exit 2 if anything FAILs; warnings alone exit 0.
    """
    loaded = _load_config(config)
    click.echo(f"clamper doctor - stack {loaded.stack_name}, {loaded.source}")
    try:
        session = session_module.build_session(profile=profile, region=region, role_arn=role_arn)
        identity = session_module.whoami(session)
    except session_module.CredentialsError as exc:
        click.echo(doctor_module.Check("credentials", doctor_module.FAIL, str(exc)).line)
        click.echo("")
        click.echo(
            "Nothing else can be checked without credentials. Clamper uses the standard "
            "chain: AWS_PROFILE, SSO, an instance or container role, or --role-arn."
        )
        raise typer.Exit(EXIT_TOOL_FAILED) from exc

    checks = [doctor_module.Check("credentials", doctor_module.PASS, identity.label)]
    if identity.is_root:
        checks.append(
            doctor_module.Check(
                "not root",
                doctor_module.WARN,
                "these are the root user's credentials. Use a role with the operator policy.",
            )
        )
    aws = Aws(session=session, clients=Clients(session), identity=identity)
    if not aws.clients.default_region:
        checks.append(
            doctor_module.Check(
                "region", doctor_module.FAIL, "no default region: pass --region or set one."
            )
        )
    else:
        checks += doctor_module.run_checks(
            loaded,
            aws.clients,
            account=identity.account,
            read_spend=_spend_reader(aws, loaded),
        )
    for check in checks:
        click.echo(check.line)
    code = doctor_module.exit_code(checks)
    click.echo("")
    click.echo("Everything checked passed." if code == 0 else "Fix each FAIL above.")
    raise typer.Exit(code)


@app.command("deploy")
def deploy_command(
    template_only: Annotated[
        bool,
        typer.Option(
            "--template-only",
            help="Write both templates and the zip to --out. No AWS calls at all.",
        ),
    ] = False,
    out: Annotated[Path, typer.Option("--out", help="Where --template-only writes.")] = Path(
        ".clamper-build"
    ),
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before deploying.")] = False,
    config: ConfigOption = None,
    profile: ProfileOption = None,
    region: RegionOption = None,
    role_arn: RoleOption = None,
) -> None:
    """Deploy the budgets, the enforcer, its ledger and its schedule.

    Two CloudFormation stacks: a bootstrap stack holding one bucket for the
    enforcer's zip, and the main stack. --template-only writes both and the
    zip to a directory and touches no AWS API, so you can read everything,
    run cfn-lint over it, and deploy it from the console instead.
    """
    loaded = _load_config(config)
    if loaded.source is None:
        raise _fail("the config has no file to pack into the enforcer.")
    text = Path(loaded.source).read_text(encoding="utf-8")
    artifacts = deploy_module.build_artifacts(loaded, text)

    if template_only:
        for path in deploy_module.write_artifacts(artifacts, out):
            click.echo(f"wrote {path}")
        click.echo("")
        names = StackNames(loaded.stack_name)
        click.echo("To deploy by hand, from the console or your own pipeline:")
        click.echo(f"  1. Create a stack named {names.bootstrap_stack} from bootstrap.yaml.")
        click.echo(f"  2. Upload {artifacts.zip_name} to its ArtifactBucket output,")
        click.echo(f"     under the key {artifacts.key}")
        click.echo(f"  3. Create a stack named {loaded.stack_name} from main.yaml, with")
        click.echo("     ArtifactBucket and ArtifactKey set to those two values, and")
        click.echo("     acknowledge that it creates a named IAM role.")
        return

    aws = _aws(profile, region, role_arn)
    where = aws.clients.default_region
    if not where:
        raise _fail("no region: pass --region or set a default. The stack lives in one region.")
    _err(
        f"Deploying stack {loaded.stack_name} in {where}, account {aws.identity.account}, "
        f"mode {loaded.mode}."
    )
    if not loaded.dry_run:
        _err(
            "mode is ENFORCE: once deployed, a breached limit stops resources. Run `clamper plan`."
        )
    if not yes and not click.confirm("Continue?", default=False, err=True):
        raise typer.Exit(EXIT_TOOL_FAILED)
    try:
        result = deploy_module.deploy(loaded, artifacts, aws.clients, say=lambda m: _err(f"  {m}"))
    except deploy_module.DeployError as exc:
        raise _fail(str(exc)) from exc
    _err("")
    _err(f"bootstrap stack {result.bootstrap}, main stack {result.main}.")
    if loaded.notifications.email:
        _err(
            f"AWS has sent a confirmation email to {loaded.notifications.email}. Nothing is "
            f"delivered until somebody clicks the link in it. `clamper doctor` checks."
        )
    else:
        _err("No notification email is configured. Nobody will hear about a clamp.")
    _err("Next: `clamper doctor`, then `clamper plan`.")


@app.command()
def demo(formats: FormatOption = None, output_dir: OutputOption = None) -> None:
    """A plan and a clamp run against a made-up account. No AWS, no config.

    The real plan, selection, ledger, actuator and reports, against bundled
    fixtures and a fake account that accepts the calls. Always exits 0.
    """
    from clamper import demo as demo_module
    from clamper.core.report import notification

    chosen = _formats(formats)
    shown = demo_module.run_demo()
    if output_dir is not None:
        _emit(shown.plan, chosen, output_dir)
        _emit(shown.run.report, chosen, output_dir)
        return
    for fmt in chosen:
        click.echo(render(shown.plan, fmt), nl=False)
        if fmt == "terminal":
            click.echo("\n" + "=" * 78 + "\n")
        click.echo(render(shown.run.report, fmt), nl=False)
    if chosen == ["terminal"]:
        click.echo("\n" + "=" * 78)
        click.echo("\nThe notification that run would send:\n")
        click.echo(f"Subject: {notification.subject(shown.run.report)}\n")
        click.echo(notification.body(shown.run.report))


def _ledger(aws: Aws, config: Config) -> Ledger:
    """The stack's ledger table, in the stack's region.

    Named from the stack name by the same function the template uses, so
    the CLI and the stack cannot disagree about where the record is.
    """
    return Ledger(aws.clients, StackNames(config.stack_name).table)


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
