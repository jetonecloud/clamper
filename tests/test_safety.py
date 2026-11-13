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

#: Non-read calls that specific modules are allowed to make, on Clamper's
#: OWN infrastructure, with the reason. Pinned, so a new one is a diff
#: somebody reviews.
#:
#: These are a genuinely different category from the actions in actions.yml.
#: `put_item` writes to the ledger table the stack created for Clamper to
#: write to; `publish` sends a notification to Clamper's own topic. Neither
#: touches a resource anybody is paying for. The strict test below has no
#: exceptions at all and covers the services that do.
ALLOWED_OWN_WRITES = {
    # The ledger. put_item only: there is no update path (an entry is
    # rewritten whole so a settle cannot half-apply) and deliberately no
    # delete, which is how "the ledger is never pruned" is a property of the
    # code rather than a promise in a document.
    "core/ledger.py": {"put_item"},
    # The notification, to the stack's own topic. A failure to send is
    # logged and never fails the run; the clamp is already recorded.
    "enforcer/notify.py": {"publish"},
    # `clamper deploy`: Clamper's own two stacks, and the enforcer zip into
    # the bucket the bootstrap stack created for it. Run by a person, with
    # the deploy policy, and never from the Lambda: deploy.py is not in the
    # zip it builds.
    "deploy.py": {"create_stack", "update_stack", "put_object"},
}

#: boto3 imports allowed outside the client factory, by exact module path.
#: `boto3.dynamodb.types` is a pure data converter with no client, session or
#: network surface — it maps Python values to DynamoDB's attribute-value
#: shape. Hand-rolling that conversion is about 25 lines of bug-prone code
#: for no gain.
ALLOWED_BOTO3_IMPORTS = {
    "core/ledger.py": {"boto3.dynamodb.types"},
}

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


def _client_calls(name: str, tree: ast.Module) -> list[tuple[int, str, str]]:
    """Every `<client-ish>.<method>()` in a module, with the service if known.

    Two ways a call is recognised as being on a boto3 client, and both rely
    on a convention this package keeps and `CONTRIBUTING.md` states: a
    client is bound to a local called `client` or something ending
    `_client`, and it is obtained from `clients.get("<service>")` with a
    literal service name.

    The literal is what makes the strict test below possible: it is how a
    static check can tell `clients.get("ec2")` from `clients.get("dynamodb")`
    and hold only the first to the no-exceptions rule.
    """
    services: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            services[target.id] = value.args[0].value

    calls: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        if owner.id != "client" and not owner.id.endswith("_client"):
            continue
        calls.append((node.lineno, node.func.attr, services.get(owner.id, "")))
    return calls


def test_no_resource_service_is_written_to_outside_the_actuator() -> None:
    """The strict version, with no exceptions anywhere.

    Every service named in `actions.yml` — ec2, autoscaling, ecs, rds,
    lambda, sagemaker — is a service somebody is paying for. A non-read call
    on a client for one of those, outside the actuator, is a second write
    path to a customer's infrastructure and there is no module that gets to
    have one.
    """
    resource_services = {action.client for action in load_registry().actions}
    offenders: list[str] = []
    for name, tree in trees():
        if name == WRITE_PATH:
            continue
        for lineno, method, service in _client_calls(name, tree):
            if service not in resource_services:
                continue
            if method in PAGINATION or method.startswith(READ_PREFIXES):
                continue
            offenders.append(f"{name}:{lineno} {service}.{method}()")
    assert offenders == [], (
        f"writes to a billed AWS service outside {WRITE_PATH}: {offenders}. "
        f"Route the call through Actuator.invoke with a declared action."
    )


def test_every_other_client_call_is_a_read_or_a_pinned_exception() -> None:
    """The brief's check, implemented: scan for boto3 client methods that are
    not Describe/Get/List.

    Clamper's own infrastructure — the ledger table, the topic, the stack —
    has to be written to by something, so this one has exceptions. They are
    per-module, per-method, and pinned in `ALLOWED_OWN_WRITES` with the
    reason beside each.
    """
    offenders: list[str] = []
    for name, tree in trees():
        if name == WRITE_PATH:
            continue
        allowed = ALLOWED_OWN_WRITES.get(name, set())
        for lineno, method, _ in _client_calls(name, tree):
            if method in PAGINATION or method.startswith(READ_PREFIXES):
                continue
            if method in allowed:
                continue
            offenders.append(f"{name}:{lineno} .{method}()")
    assert offenders == [], (
        f"non-read client calls that are not pinned exceptions: {offenders}. "
        f"If this is a write to Clamper's own infrastructure, add it to "
        f"ALLOWED_OWN_WRITES with a reason. If it is a write to somebody's "
        f"resources, route it through Actuator.invoke."
    )


def test_the_ledger_can_write_but_never_delete() -> None:
    """ "The ledger is never pruned", as a property of the code.

    This is the test that keeps it true. If a `delete_item` or a TTL
    attribute ever appears, a clamp can become unreleasable because the
    record of what to restore expired.
    """
    assert ALLOWED_OWN_WRITES["core/ledger.py"] == {"put_item"}

    forbidden = {"delete_item", "delete_table", "batch_write_item", "update_time_to_live"}
    tree = ast.parse((PACKAGE / "core" / "ledger.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        # AST rather than a text search. A text search trips on the module's
        # own prose about never deleting, which is how the first version of
        # this test failed.
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(f".{node.attr} at line {node.lineno}")
        elif isinstance(node, ast.Constant) and node.value in forbidden:
            offenders.append(f"{node.value!r} at line {node.lineno}")
    assert offenders == [], f"the ledger must never delete: {offenders}"


def test_the_pinned_exception_modules_exist() -> None:
    """Otherwise a rename silently widens the exception list to nothing,
    which looks the same as the check passing."""
    existing = {name for name, _ in trees()}
    for name in ALLOWED_OWN_WRITES:
        assert name in existing, f"{name} is pinned in ALLOWED_OWN_WRITES but does not exist"


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
        if name == CLIENT_FACTORY:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for item in names:
                if item.split(".")[0] != "boto3":
                    continue
                if item in ALLOWED_BOTO3_IMPORTS.get(name, set()):
                    continue
                importers.add(f"{name}:{node.lineno} imports {item}")
    assert importers == set(), (
        f"boto3 belongs to {CLIENT_FACTORY}, bar the pure converters pinned in "
        f"ALLOWED_BOTO3_IMPORTS. Found: {sorted(importers)}"
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
