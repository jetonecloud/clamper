"""Building and deploying Clamper's two stacks.

Two halves, and only the second touches AWS:

- **Packaging** (`build_artifacts`): the enforcer zip and both templates,
  as bytes. `clamper deploy --template-only` writes these to a directory and
  makes no AWS call at all — no `sts:GetCallerIdentity`, nothing — so a
  careful person can read the whole thing, run `cfn-lint` over it, and
  deploy it from the console instead.
- **Deploying** (`deploy`): the bootstrap stack, then the zip into its
  bucket, then the main stack, waiting on each.

This module is not in the zip it builds, and neither is the CLI or the demo.
The enforcer has no use for code that creates stacks, and its role has no
permission to.

The writes here are to Clamper's own infrastructure — two stacks and one
object in Clamper's own bucket — and they are pinned in
`tests/test_safety.py`'s `ALLOWED_OWN_WRITES` with the reason.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, WaiterError

import clamper
from clamper.core.config import CONFIG_FILENAME, Config
from clamper.core.names import StackNames
from clamper.core.session import Clients
from clamper.core.stack import bootstrap, main

#: Modules and data that exist for a person at a terminal, not for the
#: Lambda. Paths relative to the `clamper` package.
EXCLUDED_FROM_ZIP = frozenset({"cli.py", "demo.py", "deploy.py", "data/clamper.starter.yml"})
EXCLUDED_DIRS = frozenset({"__pycache__", "demo"})

#: A fixed timestamp for every zip entry, so the same inputs make the same
#: bytes and the same content-addressed key. A key that changed on every
#: build would redeploy the function on every `clamper deploy`.
ZIP_EPOCH = (2020, 1, 1, 0, 0, 0)

ARTIFACT_PREFIX = "enforcer"


@dataclass(frozen=True)
class Artifacts:
    bootstrap_template: str
    main_template: str
    zip_bytes: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.zip_bytes).hexdigest()

    @property
    def key(self) -> str:
        """Content-addressed: a new config or a new version is a new key,
        which is what makes CloudFormation update the function."""
        return f"{ARTIFACT_PREFIX}/clamper-enforcer-{clamper.__version__}-{self.digest[:16]}.zip"

    @property
    def zip_name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


# --- packaging ----------------------------------------------------------


def build_artifacts(config: Config, config_text: str) -> Artifacts:
    """Both templates and the zip. Pure: no AWS, no network, no clock."""
    return Artifacts(
        bootstrap_template=bootstrap.render_template(config.stack_name),
        main_template=main.render_template(config),
        zip_bytes=build_zip(config_text),
    )


def build_zip(config_text: str) -> bytes:
    """The enforcer's code: the `clamper` package minus the CLI, the demo and
    this module; pure-Python PyYAML; and `clamper.yml` at the root, beside
    the package, where `enforcer/handler.py` looks for it.

    PyYAML's compiled extension is left out on purpose. It is built for
    whatever machine ran `clamper deploy`, which is rarely Amazon Linux on
    the Lambda's architecture, and PyYAML falls back to pure Python when it
    is absent. boto3 is not packed: the Lambda runtime provides it.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(_entries(config_text)):
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _entries(config_text: str) -> Iterator[tuple[str, bytes]]:
    package = Path(clamper.__file__).resolve().parent
    for path in _files(package):
        relative = path.relative_to(package)
        if relative.as_posix() in EXCLUDED_FROM_ZIP:
            continue
        yield f"clamper/{relative.as_posix()}", path.read_bytes()

    import yaml

    pyyaml = Path(yaml.__file__).resolve().parent
    for path in _files(pyyaml):
        if path.suffix == ".py":
            yield f"yaml/{path.relative_to(pyyaml).as_posix()}", path.read_bytes()
    licence = _pyyaml_licence()
    if licence is not None:
        yield "yaml/LICENSE", licence

    yield CONFIG_FILENAME, config_text.encode("utf-8")


def _files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not (set(path.relative_to(root).parts[:-1]) & EXCLUDED_DIRS)
        and path.suffix in {".py", ".yml", ".yaml"}
    ]


def _pyyaml_licence() -> bytes | None:
    """PyYAML is MIT, and redistributing it means shipping its notice."""
    try:
        files = importlib.metadata.files("PyYAML") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for item in files:
        if item.name.upper().startswith("LICENSE"):
            return Path(str(item.locate())).read_bytes()
    return None


def write_artifacts(artifacts: Artifacts, out: Path) -> list[Path]:
    """`--template-only`: everything a console deploy needs, in one place."""
    out.mkdir(parents=True, exist_ok=True)
    written = [
        out / "bootstrap.yaml",
        out / "main.yaml",
        out / artifacts.zip_name,
    ]
    written[0].write_text(artifacts.bootstrap_template, encoding="utf-8")
    written[1].write_text(artifacts.main_template, encoding="utf-8")
    written[2].write_bytes(artifacts.zip_bytes)
    return written


# --- deploying ----------------------------------------------------------

#: How long to wait for a stack. A first deploy with a DynamoDB table and
#: point-in-time recovery takes a few minutes; twenty is generous.
WAIT_DELAY_SECONDS = 10
WAIT_MAX_ATTEMPTS = 120

#: The main stack names its role, so CloudFormation has to be told it may.
CAPABILITIES = ["CAPABILITY_NAMED_IAM"]

NO_UPDATES = "No updates are to be performed"


class DeployError(RuntimeError):
    """A deploy step failed. The message says which, and what state it left."""


@dataclass(frozen=True)
class Deployed:
    bucket: str
    key: str
    uploaded: bool
    bootstrap: str
    main: str


def deploy(
    config: Config,
    artifacts: Artifacts,
    clients: Clients,
    *,
    say: Callable[[str], None] = lambda _: None,
) -> Deployed:
    """Bootstrap stack, then the zip, then the main stack. Each waits.

    Stops at the first failure: a main stack pointed at a zip that never
    uploaded would fail anyway, later and less clearly.
    """
    names = StackNames(config.stack_name)
    say(f"bootstrap stack {names.bootstrap_stack}")
    first = _apply_stack(clients, names.bootstrap_stack, artifacts.bootstrap_template, [], [])
    bucket = _output(clients, names.bootstrap_stack, "ArtifactBucket")

    say(f"enforcer zip s3://{bucket}/{artifacts.key}")
    uploaded = _upload(clients, bucket, artifacts)

    say(f"main stack {config.stack_name}")
    second = _apply_stack(
        clients,
        config.stack_name,
        artifacts.main_template,
        [
            {"ParameterKey": "ArtifactBucket", "ParameterValue": bucket},
            {"ParameterKey": "ArtifactKey", "ParameterValue": artifacts.key},
        ],
        CAPABILITIES,
    )
    return Deployed(
        bucket=bucket, key=artifacts.key, uploaded=uploaded, bootstrap=first, main=second
    )


def _apply_stack(
    clients: Clients,
    name: str,
    body: str,
    parameters: list[dict[str, str]],
    capabilities: list[str],
) -> str:
    """Create or update one stack and wait. Returns what happened."""
    client = clients.get("cloudformation")
    exists = _stack_status(clients, name) is not None
    request: dict[str, Any] = {
        "StackName": name,
        "TemplateBody": body,
        "Parameters": parameters,
        "Capabilities": capabilities,
        "Tags": [{"Key": "clamper:component", "Value": "stack"}],
    }
    try:
        if exists:
            client.update_stack(**request)
            waiter_name, done = "stack_update_complete", "updated"
        else:
            client.create_stack(**request, EnableTerminationProtection=False)
            waiter_name, done = "stack_create_complete", "created"
    except ClientError as exc:
        code, message = _error(exc)
        if code == "ValidationError" and NO_UPDATES in message:
            return "unchanged"
        raise DeployError(
            f"could not {'update' if exists else 'create'} {name}: {code} - {message}"
        ) from exc
    except BotoCoreError as exc:
        raise DeployError(f"could not reach CloudFormation for {name}: {exc}") from exc

    waiter = client.get_waiter(waiter_name)
    try:
        waiter.wait(
            StackName=name,
            WaiterConfig={"Delay": WAIT_DELAY_SECONDS, "MaxAttempts": WAIT_MAX_ATTEMPTS},
        )
    except WaiterError as exc:
        status = _stack_status(clients, name) or "unknown"
        raise DeployError(
            f"{name} did not finish: it is {status}. The stack's Events tab in the "
            f"CloudFormation console says which resource failed and why."
        ) from exc
    return done


def _stack_status(clients: Clients, name: str) -> str | None:
    client = clients.get("cloudformation")
    try:
        stacks = client.describe_stacks(StackName=name).get("Stacks", [])
    except ClientError as exc:
        code, message = _error(exc)
        if code == "ValidationError" and "does not exist" in message:
            return None
        raise DeployError(f"could not read stack {name}: {code} - {message}") from exc
    if not stacks:
        return None
    status = str(stacks[0].get("StackStatus") or "")
    if status == "ROLLBACK_COMPLETE":
        # A first create that rolled back cannot be updated, only deleted.
        # Deleting is a person's call, not this tool's.
        raise DeployError(
            f"{name} is ROLLBACK_COMPLETE from an earlier failed create. CloudFormation "
            f"cannot update it; delete it in the console, read why it failed first, and "
            f"run `clamper deploy` again."
        )
    return status


def _output(clients: Clients, stack: str, key: str) -> str:
    client = clients.get("cloudformation")
    stacks = client.describe_stacks(StackName=stack).get("Stacks", [])
    for item in (stacks[0].get("Outputs") or []) if stacks else []:
        if item.get("OutputKey") == key:
            return str(item.get("OutputValue"))
    raise DeployError(f"stack {stack} has no {key} output")


def _upload(clients: Clients, bucket: str, artifacts: Artifacts) -> bool:
    """Put the zip, unless that exact key is already there.

    The key is the content's hash, so an object under it is this zip.
    """
    client = clients.get("s3")
    try:
        client.head_object(Bucket=bucket, Key=artifacts.key)
        return False
    except ClientError as exc:
        code, _ = _error(exc)
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise DeployError(f"could not check s3://{bucket}/{artifacts.key}: {code}") from exc
    try:
        client.put_object(
            Bucket=bucket,
            Key=artifacts.key,
            Body=artifacts.zip_bytes,
            ContentType="application/zip",
            ServerSideEncryption="AES256",
        )
    except (ClientError, BotoCoreError) as exc:
        raise DeployError(f"could not upload the enforcer zip to s3://{bucket}: {exc}") from exc
    return True


def _error(exc: ClientError) -> tuple[str, str]:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "ClientError"), str(error.get("Message") or "")
