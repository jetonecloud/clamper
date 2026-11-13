"""`clamper deploy`: the stack order, against botocore's Stubber.

Stubber validates every request against the real CloudFormation and S3
models, which is the thing worth checking here: a wrong parameter name on
`create_stack` would otherwise be found by the first person to deploy.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber
from support import REGION
from typer.testing import CliRunner

from clamper import cli, deploy
from clamper.core.config import loads
from clamper.core.session import Clients

CONFIG_TEXT = (
    "version: 1\nmode: dry-run\nlimits:\n  - id: whole-account\n    amount_usd: 100\n"
    "notifications:\n  email: ops@example.com\n"
)
BUCKET = "clamper-bootstrap-artifactbucket-abc123"
NOW = "2026-11-13T00:00:00Z"


@pytest.fixture()
def stubbed() -> Iterator[tuple[Clients, Stubber, Stubber]]:
    session = boto3.Session(region_name=REGION)
    clients = Clients(session, default_region=REGION)
    cfn = session.client("cloudformation", region_name=REGION)
    s3 = session.client("s3", region_name=REGION)
    clients.stub("cloudformation", cfn)
    clients.stub("s3", s3)
    with Stubber(cfn) as cfn_stub, Stubber(s3) as s3_stub:
        yield clients, cfn_stub, s3_stub
        cfn_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()


def missing(stub: Stubber, name: str) -> None:
    stub.add_client_error(
        "describe_stacks",
        service_error_code="ValidationError",
        service_message=f"Stack with id {name} does not exist",
        expected_params={"StackName": name},
    )


def stack(
    stub: Stubber, name: str, status: str, outputs: list[dict[str, str]] | None = None
) -> None:
    body: dict[str, Any] = {"StackName": name, "CreationTime": NOW, "StackStatus": status}
    if outputs:
        body["Outputs"] = outputs
    stub.add_response("describe_stacks", {"Stacks": [body]}, {"StackName": name})


def test_a_first_deploy_creates_both_stacks_in_order(stubbed: Any) -> None:
    clients, cfn, s3 = stubbed
    config = loads(CONFIG_TEXT)
    artifacts = deploy.build_artifacts(config, CONFIG_TEXT)
    out = [{"OutputKey": "ArtifactBucket", "OutputValue": BUCKET}]

    missing(cfn, "clamper-bootstrap")
    cfn.add_response(
        "create_stack",
        {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/clamper-bootstrap/1"},
        {
            "StackName": "clamper-bootstrap",
            "TemplateBody": artifacts.bootstrap_template,
            "Parameters": [],
            "Capabilities": [],
            "Tags": ANY,
            "EnableTerminationProtection": False,
        },
    )
    stack(cfn, "clamper-bootstrap", "CREATE_COMPLETE")  # the waiter
    stack(cfn, "clamper-bootstrap", "CREATE_COMPLETE", out)  # the output
    s3.add_client_error("head_object", service_error_code="404", http_status_code=404)
    s3.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": artifacts.key,
            "Body": artifacts.zip_bytes,
            "ContentType": "application/zip",
            "ServerSideEncryption": "AES256",
        },
    )
    missing(cfn, "clamper")
    cfn.add_response(
        "create_stack",
        {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/clamper/2"},
        {
            "StackName": "clamper",
            "TemplateBody": artifacts.main_template,
            "Parameters": [
                {"ParameterKey": "ArtifactBucket", "ParameterValue": BUCKET},
                {"ParameterKey": "ArtifactKey", "ParameterValue": artifacts.key},
            ],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "Tags": ANY,
            "EnableTerminationProtection": False,
        },
    )
    stack(cfn, "clamper", "CREATE_COMPLETE")

    result = deploy.deploy(config, artifacts, clients)
    assert (result.bootstrap, result.main, result.uploaded) == ("created", "created", True)


def test_a_redeploy_with_nothing_new_changes_nothing(stubbed: Any) -> None:
    clients, cfn, s3 = stubbed
    config = loads(CONFIG_TEXT)
    artifacts = deploy.build_artifacts(config, CONFIG_TEXT)
    no_updates = "No updates are to be performed."

    stack(cfn, "clamper-bootstrap", "UPDATE_COMPLETE")
    cfn.add_client_error(
        "update_stack", service_error_code="ValidationError", service_message=no_updates
    )
    stack(
        cfn,
        "clamper-bootstrap",
        "UPDATE_COMPLETE",
        [{"OutputKey": "ArtifactBucket", "OutputValue": BUCKET}],
    )
    s3.add_response("head_object", {}, {"Bucket": BUCKET, "Key": artifacts.key})
    stack(cfn, "clamper", "UPDATE_COMPLETE")
    cfn.add_client_error(
        "update_stack", service_error_code="ValidationError", service_message=no_updates
    )

    result = deploy.deploy(config, artifacts, clients)
    assert (result.bootstrap, result.main, result.uploaded) == ("unchanged", "unchanged", False)


def test_a_rolled_back_first_create_is_left_for_a_person(stubbed: Any) -> None:
    clients, cfn, _ = stubbed
    config = loads(CONFIG_TEXT)
    stack(cfn, "clamper-bootstrap", "ROLLBACK_COMPLETE")
    with pytest.raises(deploy.DeployError, match="delete it in the console"):
        deploy.deploy(config, deploy.build_artifacts(config, CONFIG_TEXT), clients)


def test_template_only_makes_no_aws_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_aws(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("--template-only reached for credentials")

    monkeypatch.setattr(cli, "_aws", no_aws)
    path = tmp_path / "clamper.yml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli.app, ["deploy", "--template-only", "--out", str(out), "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "bootstrap.yaml").is_file() and (out / "main.yaml").is_file()
    assert len(list(out.glob("clamper-enforcer-*.zip"))) == 1


def test_deploy_asks_first_and_stops_on_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_aws: Any
) -> None:
    def no_deploy(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deployed without a yes")

    monkeypatch.setattr(deploy, "deploy", no_deploy)
    path = tmp_path / "clamper.yml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["deploy", "--config", str(path)], input="n\n")
    assert result.exit_code == 2
    assert "Deploying stack clamper" in result.stderr


def test_deploy_reminds_about_the_confirmation_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_aws: Any
) -> None:
    monkeypatch.setattr(
        deploy,
        "deploy",
        lambda *args, **kwargs: deploy.Deployed(BUCKET, "k", True, "created", "created"),
    )
    path = tmp_path / "clamper.yml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["deploy", "--yes", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "confirmation email to ops@example.com" in result.stderr
