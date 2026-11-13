"""The action registry, and the rows it refuses to load.

The refusals matter more than the loads. `actions.yml` is the complete list
of writes Clamper can make, so a row that should not be there getting in is
the first step of every bad outcome this tool is designed around.
"""

from __future__ import annotations

import pytest

from clamper.core import actions as actions_module
from clamper.core.actions import (
    PHASE_CLAMP,
    PHASE_RELEASE,
    PHASE_RELEASE_ABSENT,
    DestructiveAction,
    RegistryError,
    UnknownAction,
    load_registry,
    loads,
)

# A minimal valid document, to be broken one field at a time.
GOOD = """
version: 1
destructive_verbs: [terminate, delete]
destructive_verb_exceptions: []
actions:
  - id: thing
    title: A thing
    client: ec2
    identifier: ThingId
    arn_pattern: "arn:aws:ec2:*:{account}:thing/*"
    tag_condition: "aws:ResourceTag/"
    reversible: true
    discover: [ec2:DescribeThings]
    records: [state]
    clamp:
      method: stop_things
      iam: ec2:StopThings
      params: [ThingIds]
    release:
      method: start_things
      iam: ec2:StartThings
      params: [ThingIds]
"""


def broken(*replacements: tuple[str, str]) -> str:
    """`GOOD` with literal substitutions applied.

    Literal pairs rather than keyword arguments: the strings being replaced
    contain colons and slashes, and an earlier version of this helper encoded
    them in identifiers and silently replaced nothing, so three refusal tests
    passed for the wrong reason.
    """
    text = GOOD
    for old, new in replacements:
        assert old in text, f"{old!r} is not in the fixture; the test would prove nothing"
        text = text.replace(old, new)
    return text


# --- the shipped file ---------------------------------------------------


def test_the_shipped_registry_loads() -> None:
    """If this fails, the package does not import for anybody."""
    registry = load_registry()
    assert registry.ids() == {
        "ec2-instance",
        "autoscaling-group",
        "ecs-service",
        "rds-instance",
        "aurora-cluster",
        "lambda-function",
        "sagemaker-notebook",
    }


def test_the_shipped_registry_is_the_documented_set() -> None:
    """Pin the methods, so adding a write has to be a deliberate edit.

    This is the same reasoning as Crosswind pinning its subcommand
    allowlist: the set is small, somebody should look at it changing, and a
    diff on this assertion is that somebody looking.
    """
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


def test_every_shipped_action_is_reversible() -> None:
    registry = load_registry()
    for action in registry.actions:
        assert action.release is not None, action.id
        assert action.records, action.id


def test_every_shipped_action_records_something_to_restore() -> None:
    """Nothing recorded means nothing to put back, whatever the API allows."""
    for action in load_registry().actions:
        assert action.records, f"{action.id} records no previous state"


def test_only_lambda_needs_a_release_absent() -> None:
    """The asymmetric case is one row, and should stay one row."""
    with_absent = [a.id for a in load_registry().actions if a.release_absent is not None]
    assert with_absent == ["lambda-function"]


def test_the_only_destructive_exception_is_lambda_concurrency() -> None:
    """One named exception. A second one needs this test changed on purpose."""
    assert load_registry().destructive_verb_exceptions == {"lambda:DeleteFunctionConcurrency"}


def test_actions_that_aws_undoes_itself_are_marked() -> None:
    """RDS restarts a stopped instance after 7 days; the reconcile re-stops it.

    If this flag is lost, the re-stop never happens and a database quietly
    comes back up a week into a cost incident.
    """
    registry = load_registry()
    assert registry.get("rds-instance").restart_after_days == 7
    assert registry.get("aurora-cluster").restart_after_days == 7
    assert not registry.get("ec2-instance").restarts_itself


def test_autoscaling_uses_its_own_tag_condition_key() -> None:
    """`aws:ResourceTag` on this action is not an error, it just never matches.

    Which means the permission silently does nothing, which is the worst
    available failure mode for an IAM condition.
    """
    registry = load_registry()
    assert registry.get("autoscaling-group").tag_condition == "autoscaling:ResourceTag/"
    assert registry.get("ec2-instance").tag_condition == "aws:ResourceTag/"


def test_autoscaling_clamp_sets_min_size_as_well_as_desired() -> None:
    """Desired alone leaves the group under its minimum and it scales back up."""
    clamp = load_registry().get("autoscaling-group").clamp
    assert "MinSize" in clamp.params
    assert "DesiredCapacity" in clamp.params


def test_release_restores_max_size_too() -> None:
    """The clamp does not change MaxSize, but a release that omits it from the
    call would leave whatever the clamp's partial update left behind."""
    release = load_registry().get("autoscaling-group").release
    assert set(release.params) == {"AutoScalingGroupName", "MinSize", "MaxSize", "DesiredCapacity"}


def test_discover_actions_are_all_reads() -> None:
    """Discovery permissions are granted unscoped, so a write among them would
    be a write Clamper can make that the actuator never sees."""
    for action in load_registry().actions:
        for item in action.discover:
            verb = item.partition(":")[2]
            assert verb.startswith(("Describe", "Get", "List")), f"{action.id}: {item}"


def test_every_arn_pattern_is_account_scoped() -> None:
    for action in load_registry().actions:
        assert "{account}" in action.arn_pattern, action.id
        assert action.arn_pattern.startswith("arn:"), action.id


def test_notes_are_folded_to_single_lines() -> None:
    """Notes are printed under a target in `clamper plan`, so a stray newline
    from the YAML would break the layout."""
    for action in load_registry().actions:
        for note in action.notes:
            assert "\n" not in note
            assert note == " ".join(note.split())


def test_every_action_carries_notes() -> None:
    """Every one of these has a limitation a person needs told before they
    rely on the clamp, not after."""
    for action in load_registry().actions:
        assert action.notes, f"{action.id} has no notes"


# --- refusals -----------------------------------------------------------


def test_a_terminate_verb_is_refused() -> None:
    with pytest.raises(DestructiveAction) as excinfo:
        loads(
            broken(("stop_things", "terminate_things"), ("ec2:StopThings", "ec2:TerminateThings"))
        )
    assert "terminate" in str(excinfo.value)


def test_a_delete_verb_is_refused() -> None:
    with pytest.raises(DestructiveAction):
        loads(broken(("stop_things", "delete_things"), ("ec2:StopThings", "ec2:DeleteThings")))


def test_a_destructive_iam_action_is_refused_even_with_an_innocent_method() -> None:
    """Both spellings are checked, because either being wrong is enough.

    A row whose method and IAM action disagree is itself suspicious, and the
    IAM action is the one a reader of the generated policy sees.
    """
    with pytest.raises(DestructiveAction):
        loads(broken(("ec2:StopThings", "ec2:TerminateThings")))


def test_a_destructive_method_is_refused_even_with_an_innocent_iam_action() -> None:
    with pytest.raises(DestructiveAction):
        loads(broken(("stop_things", "terminate_things")))


def test_a_destructive_release_is_refused() -> None:
    """The inverse is checked as hard as the clamp. A release that terminates
    is not a release."""
    with pytest.raises(DestructiveAction):
        loads(broken(("start_things", "delete_things"), ("ec2:StartThings", "ec2:DeleteThings")))


def test_an_exception_must_be_an_exact_iam_action() -> None:
    """A pattern in the exception list would be a hole, and the exception list
    exists precisely to be narrow."""
    with pytest.raises(RegistryError) as excinfo:
        loads(
            GOOD.replace(
                "destructive_verb_exceptions: []", 'destructive_verb_exceptions: ["ec2:Delete*"]'
            )
        )
    assert "literal IAM action" in str(excinfo.value)


def test_a_named_exception_is_allowed_through() -> None:
    """The mechanism has to work, or the one real exception could not exist."""
    registry = loads(
        broken(
            ("start_things", "delete_thing_setting"),
            ("ec2:StartThings", "ec2:DeleteThingSetting"),
            (
                "destructive_verb_exceptions: []",
                'destructive_verb_exceptions: ["ec2:DeleteThingSetting"]',
            ),
        )
    )
    assert registry.get("thing").release.method == "delete_thing_setting"


def test_a_row_without_reversible_true_is_refused() -> None:
    with pytest.raises(DestructiveAction) as excinfo:
        loads(GOOD.replace("reversible: true", "reversible: false"))
    assert "reversible" in str(excinfo.value)


def test_a_row_with_reversible_missing_is_refused() -> None:
    """Absent is not the same as false, and both have to fail. A new row
    written by copying an old one and dropping a field should not default to
    permitted."""
    with pytest.raises(DestructiveAction):
        loads(GOOD.replace("    reversible: true\n", ""))


def test_an_empty_destructive_verb_list_is_refused() -> None:
    """An empty list makes every refusal a no-op while still looking like a
    check, which is worse than having no check."""
    with pytest.raises(RegistryError) as excinfo:
        loads(GOOD.replace("destructive_verbs: [terminate, delete]", "destructive_verbs: []"))
    assert "no-op" in str(excinfo.value)


def test_a_write_in_discover_is_refused() -> None:
    with pytest.raises(DestructiveAction) as excinfo:
        loads(broken(("ec2:DescribeThings", "ec2:StopThings")))
    assert "Describe/Get/List" in str(excinfo.value)


def test_records_cannot_be_empty() -> None:
    with pytest.raises(RegistryError) as excinfo:
        loads(GOOD.replace("records: [state]", "records: []"))
    assert "irreversible in practice" in str(excinfo.value)


def test_discover_cannot_be_empty() -> None:
    with pytest.raises(RegistryError):
        loads(GOOD.replace("discover: [ec2:DescribeThings]", "discover: []"))


def test_params_cannot_be_empty() -> None:
    """The actuator checks a call's keys against this list, so an empty list
    would reject every call — a clamp that fails for an invisible reason."""
    with pytest.raises(RegistryError) as excinfo:
        loads(GOOD.replace("params: [ThingIds]\n    release", "params: []\n    release"))
    assert "reject every call" in str(excinfo.value)


def test_an_unscoped_arn_pattern_is_refused() -> None:
    with pytest.raises(RegistryError) as excinfo:
        loads(broken(('"arn:aws:ec2:*:{account}:thing/*"', '"arn:aws:ec2:*:*:thing/*"')))
    assert "{account}" in str(excinfo.value)


def test_a_tag_condition_without_a_trailing_slash_is_refused() -> None:
    """The tag key is appended to it, so `aws:ResourceTag` with no slash
    produces `aws:ResourceTagclamper:managed`, which matches nothing."""
    with pytest.raises(RegistryError) as excinfo:
        loads(broken(('"aws:ResourceTag/"', '"aws:ResourceTag"')))
    assert "end with" in str(excinfo.value)


def test_a_duplicate_id_is_refused() -> None:
    doubled = GOOD + GOOD.split("actions:")[1]
    with pytest.raises(RegistryError) as excinfo:
        loads(doubled)
    assert "twice" in str(excinfo.value)


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(RegistryError) as excinfo:
        loads(GOOD.replace("    title: A thing", "    title: A thing\n    danger: yes"))
    assert "danger" in str(excinfo.value)


def test_a_wrong_version_is_refused() -> None:
    with pytest.raises(RegistryError):
        loads(GOOD.replace("version: 1", "version: 2"))


@pytest.mark.parametrize("text", ["", "[]", "not: a: valid: document:"])
def test_a_malformed_document_is_refused(text: str) -> None:
    with pytest.raises(RegistryError):
        loads(text)


# --- lookups ------------------------------------------------------------


def test_an_unknown_action_id_raises() -> None:
    """How a ledger entry or config naming a retired type surfaces as an
    error rather than as a silent no-op."""
    with pytest.raises(UnknownAction) as excinfo:
        load_registry().get("ec2-nonsense")
    assert "Available" in str(excinfo.value)


def test_method_for_is_the_only_way_to_get_a_method() -> None:
    registry = load_registry()
    assert registry.method_for("ec2-instance", PHASE_CLAMP) == "stop_instances"
    assert registry.method_for("ec2-instance", PHASE_RELEASE) == "start_instances"


def test_release_absent_raises_where_there_is_none() -> None:
    with pytest.raises(UnknownAction) as excinfo:
        load_registry().get("ec2-instance").operation(PHASE_RELEASE_ABSENT)
    assert "release_absent" in str(excinfo.value)


def test_an_unknown_phase_raises() -> None:
    with pytest.raises(UnknownAction):
        load_registry().get("ec2-instance").operation("obliterate")


def test_enabled_returns_declaration_order_not_config_order() -> None:
    """So two configs listing the same types produce the same plan and the
    same report, whatever order they wrote them in."""
    registry = load_registry()
    one = registry.enabled(("rds-instance", "ec2-instance"))
    two = registry.enabled(("ec2-instance", "rds-instance"))
    assert [a.id for a in one] == [a.id for a in two] == ["ec2-instance", "rds-instance"]


def test_the_registry_is_cached_but_clearable() -> None:
    """Cached because it is read on every code path; clearable because the
    tests above load deliberately broken documents."""
    assert load_registry() is load_registry()
    actions_module.load_registry.cache_clear()
    assert load_registry().ids()
