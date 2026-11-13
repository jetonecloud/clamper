"""The tests that have to pass for Clamper's central claims to be true.

If any test in this file fails, Clamper can write to someone's
infrastructure in a way nobody declared, and it should not ship.

They are deliberately paranoid and deliberately **structural**: most assert
something about the shape of the code rather than about one function's
behaviour. Crosswind's equivalent file explains why, and it applies exactly
here — the risk is not the one write path changing, it is a *second* one
appearing.

The claims being defended:

1. Every mutating AWS call goes through `core/actuator.py`.
2. No boto3 method name exists in the Python source at all. They live only
   in `actions.yml`, so the actuator's dispatch cannot be fed one.
3. No known destructive AWS operation appears anywhere in the package.
4. boto3 clients are constructed in `core/session.py` only.
5. The actuator's allowlist is the registry's, and the set is pinned.
6. Nothing under `core/` or `enforcer/` imports a CLI-only dependency, since
   neither is in the Lambda zip.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from clamper.core import actuator as actuator_module
from clamper.core.actions import load_registry

PACKAGE = Path(actuator_module.__file__).parent.parent

#: The one module that may construct a boto3 client.
CLIENT_FACTORY = "core/session.py"

#: The one module that may dispatch a mutating call.
WRITE_PATH = "core/actuator.py"

#: Read-only boto3 prefixes, plus the paginator helpers. An attribute call on
#: a client has to start with one of these outside the actuator.
READ_PREFIXES = (
    "describe_",
    "get_",
    "list_",
    "head_",
    "lookup_",
    "query",
    "scan",
    "batch_get_",
)
PAGINATION = {"get_paginator", "can_paginate", "paginate"}

#: Destructive AWS operations, by their real boto3 names. Not a pattern: a
#: pattern over verb prefixes would flag `release_call` and `remove_prefix`
#: and get itself suppressed. These are the calls that would actually lose
#: somebody data, named one at a time so the list is reviewable.
DESTRUCTIVE_OPERATIONS = frozenset(
    {
        # EC2 and Auto Scaling
        "terminate_instances",
        "terminate_instance_in_auto_scaling_group",
        "delete_auto_scaling_group",
        "delete_launch_configuration",
        "delete_volume",
        "delete_snapshot",
        "deregister_image",
        "release_address",
        "detach_volume",
        "reboot_instances",
        # Containers
        "delete_service",
        "delete_cluster",
        "deregister_task_definition",
        "delete_nodegroup",
        # Databases
        "delete_db_instance",
        "delete_db_cluster",
        "delete_db_snapshot",
        "delete_db_cluster_snapshot",
        "reboot_db_instance",
        "delete_table",
        "delete_backup",
        # Serverless and ML
        "delete_function",
        "delete_event_source_mapping",
        "delete_endpoint",
        "delete_endpoint_config",
        "delete_notebook_instance",
        "delete_model",
        # Infrastructure
        "delete_stack",
        "delete_bucket",
        "delete_object",
        "delete_objects",
        "delete_topic",
        "delete_role",
        "delete_role_policy",
        "put_role_policy",
        "attach_role_policy",
    }
)


def python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def trees() -> list[tuple[str, ast.Module]]:
    return [
        (relative(path), ast.parse(path.read_text(encoding="utf-8"))) for path in python_files()
    ]


def test_there_are_python_files_to_check() -> None:
    """A glob that matches nothing makes every test below vacuously pass.

    Crosswind does not have this test and should; a refactor that moved the
    package would have turned its whole safety suite green and empty.
    """
    assert len(python_files()) > 5


# --- 1 and 2: the write path --------------------------------------------


def test_no_boto3_method_name_appears_in_the_source() -> None:
    """The strongest form of "one write path".

    The actuator resolves a method with `getattr(client, name)` where `name`
    comes from the registry, so no declared method name needs to exist in
    Python at all — and none does. A literal `"stop_instances"` anywhere in
    the package would be either a second dispatch or a comment that should
    be reworded; either way a human should look at it.

    This is stronger than the original design, which was "the names appear
    in actuator.py and nowhere else". Resolving by `getattr` from the
    registry removed the need for them to appear even there.
    """
    methods = load_registry().methods
    offenders: list[str] = []
    for name, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in methods:
                offenders.append(f"{name}:{node.lineno} literal {node.value!r}")
            elif isinstance(node, ast.Attribute) and node.attr in methods:
                offenders.append(f"{name}:{node.lineno} attribute .{node.attr}")
    assert offenders == [], (
        f"boto3 method names belong in actions.yml and nowhere else: {offenders}"
    )


def test_getattr_dispatch_happens_only_in_the_actuator() -> None:
    """`getattr(client, something)` is the one way to call an arbitrary
    method, so there is one place it may appear."""
    offenders: list[str] = []
    for name, tree in trees():
        if name == WRITE_PATH:
            continue
        for node in ast.walk(tree):
            # A literal second argument is a field lookup, not a dispatch —
            # `getattr(exc, "response", None)` is fine anywhere.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and not isinstance(node.args[1], ast.Constant)
            ):
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"dynamic getattr outside the actuator: {offenders}"


def test_every_client_call_outside_the_actuator_is_a_read() -> None:
    """The brief's check, implemented: scan for boto3 client methods that are
    not Describe/Get/List.

    Relies on a convention the package keeps and `CONTRIBUTING.md` states: a
    boto3 client is bound to a local called `client` or something ending
    `_client`. That makes "a call on a client" statically visible, which is
    what lets this test exist at all.
    """
    offenders: list[str] = []
    for name, tree in trees():
        if name == WRITE_PATH:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            if not isinstance(target, ast.Name):
                continue
            if target.id != "client" and not target.id.endswith("_client"):
                continue
            method = node.func.attr
            if method in PAGINATION or method.startswith(READ_PREFIXES):
                continue
            offenders.append(f"{name}:{node.lineno} {target.id}.{method}()")
    assert offenders == [], (
        f"non-read calls on a boto3 client outside {WRITE_PATH}: {offenders}. "
        f"Route the call through Actuator.invoke with a declared action."
    )


# --- 3: destructive operations ------------------------------------------


def test_no_destructive_operation_appears_anywhere() -> None:
    """Crude on purpose, and the crudeness is the value.

    A `terminate_instances` anywhere in this package — in a call, a string, a
    dict key — is something a human should look at before it ships.
    """
    offenders: list[str] = []
    for name, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in DESTRUCTIVE_OPERATIONS:
                offenders.append(f"{name}:{node.lineno} literal {node.value!r}")
            elif isinstance(node, ast.Attribute) and node.attr in DESTRUCTIVE_OPERATIONS:
                offenders.append(f"{name}:{node.lineno} .{node.attr}")
    assert offenders == [], f"destructive AWS operations in the package: {offenders}"


def test_the_registry_declares_no_destructive_operation() -> None:
    """Belt and braces over the registry's own load-time refusal, and a
    cross-check between two independently written lists."""
    declared = load_registry().methods
    assert declared & DESTRUCTIVE_OPERATIONS == frozenset()


def test_no_process_spawning_api_is_used() -> None:
    """Clamper has no reason to run a subprocess, so it may not.

    Crosswind needs exactly one, for the Terraform binary. Clamper needs
    none, which means the whole category can be banned rather than
    constrained.
    """
    banned = {"system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnl"}
    offenders: list[str] = []
    for name, tree in trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "subprocess":
                        offenders.append(f"{name}:{node.lineno} import subprocess")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "subprocess":
                    offenders.append(f"{name}:{node.lineno} from subprocess")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in banned
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                offenders.append(f"{name}:{node.lineno} os.{node.attr}")
    assert offenders == []


def test_eval_and_exec_are_not_used() -> None:
    """`clamper.yml` is untrusted input and so is a ledger entry. Neither is
    anywhere near an evaluator, and this is how it stays that way."""
    offenders: list[str] = []
    for name, tree in trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "compile", "__import__"}
            ):
                offenders.append(f"{name}:{node.lineno} {node.func.id}")
    assert offenders == []


def test_yaml_is_only_ever_loaded_safely() -> None:
    """`yaml.load` with a loader that constructs objects is remote code
    execution via a config file."""
    offenders: list[str] = []
    for name, tree in trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "yaml"
                and node.attr in {"load", "unsafe_load", "full_load", "load_all"}
            ):
                offenders.append(f"{name}:{node.lineno} yaml.{node.attr}")
    assert offenders == []


# --- 4: client construction ---------------------------------------------


def test_boto3_clients_are_constructed_in_one_module() -> None:
    """A module that can build its own client can call anything on it, which
    makes the allowlist decoration."""
    offenders: list[str] = []
    for name, tree in trees():
        if name == CLIENT_FACTORY:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"client", "resource"}
            ):
                offenders.append(f"{name}:{node.lineno} .{node.func.attr}()")
    assert offenders == [], f"boto3 clients built outside {CLIENT_FACTORY}: {offenders}"


def test_boto3_is_imported_only_where_it_is_needed() -> None:
    """Not a safety property on its own, but a cheap way to notice a module
    growing an AWS dependency it should not have."""
    importers = set()
    for name, tree in trees():
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(item.split(".")[0] == "boto3" for item in names):
                importers.add(name)
    assert importers == {CLIENT_FACTORY}, (
        f"boto3 should be imported only by {CLIENT_FACTORY}, found: {sorted(importers)}"
    )


# --- 5: the allowlist ---------------------------------------------------


def test_the_actuator_allowlist_is_the_registrys() -> None:
    from clamper.core.session import Clients

    class _NoSession:
        region_name = "us-east-1"

    actuator = actuator_module.Actuator(Clients(_NoSession()), dry_run=True)
    assert actuator.allowed_methods == load_registry().methods


def test_the_allowlist_is_pinned() -> None:
    """Twelve calls. Widening this has to be a deliberate edit to a test,
    which is a diff somebody reviews."""
    assert load_registry().methods == {
        "stop_instances",
        "start_instances",
        "update_auto_scaling_group",
        "update_service",
        "stop_db_instance",
        "start_db_instance",
        "stop_db_cluster",
        "start_db_cluster",
        "put_function_concurrency",
        "delete_function_concurrency",
        "stop_notebook_instance",
        "start_notebook_instance",
    }


def test_every_allowlisted_write_has_an_inverse_in_the_allowlist() -> None:
    """Reversibility, checked as a property of the set rather than of a row."""
    registry = load_registry()
    for action in registry.actions:
        assert action.clamp.method in registry.methods
        assert action.release.method in registry.methods


# --- 6: the Lambda's import surface -------------------------------------

#: Dependencies that exist for the command line and are deliberately not in
#: the deployment zip. A module under core/ or enforcer/ importing one would
#: fail at cold start, which is a bad place to find out.
CLI_ONLY = {"rich", "typer", "click"}

LAMBDA_PACKAGES = ("core/", "enforcer/")


def test_nothing_the_lambda_imports_depends_on_a_cli_package() -> None:
    offenders: list[str] = []
    for name, tree in trees():
        if not name.startswith(LAMBDA_PACKAGES):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for item in names:
                if item.split(".")[0] in CLI_ONLY:
                    offenders.append(f"{name}:{node.lineno} imports {item}")
    assert offenders == [], (
        f"rich/typer/click are not in the Lambda zip, so importing one here fails at "
        f"cold start: {offenders}"
    )


def test_the_enforcer_does_not_import_the_cli() -> None:
    """A circular dependency that would also drag typer into the zip."""
    offenders: list[str] = []
    for name, tree in trees():
        if not name.startswith("enforcer/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {
                "clamper.cli",
                "clamper.demo",
            }:
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("package", LAMBDA_PACKAGES)
def test_the_lambda_packages_exist(package: str) -> None:
    """Otherwise the two tests above check nothing."""
    assert any(name.startswith(package) for name, _ in trees()), package
