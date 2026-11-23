"""`clamper.yml`, and everything it refuses.

This file decides what Clamper may stop, so the tests that matter are the
ones asserting a bad config fails rather than being interpreted generously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clamper.core.actions import load_registry
from clamper.core.config import (
    MODE_DRY_RUN,
    MODE_ENFORCE,
    UNTAGGED_ACKNOWLEDGEMENT,
    ConfigError,
    find_config,
    load,
    loads,
)

MINIMAL = """
version: 1
limits:
  - id: whole-account
    amount_usd: 500
"""

FULL = """
version: 1
mode: enforce
stack_name: clamper-prod
regions: [us-east-1, eu-west-2]

limits:
  - id: whole-account
    amount_usd: 500
    scope:
      kind: account
    warn_percent: 75
    clamp_percent: 110
    notification_type: actual
  - id: ml-team
    amount_usd: 2000
    scope:
      kind: tag
      tag: CostCentre
      value: ml

targets:
  managed_tag: cost:managed
  managed_tag_value: "yes"
  protect_tag: cost:protect
  types: [ec2-instance, rds-instance]
  selectors:
    - type: ec2-instance
      ids: [i-0123456789abcdef0]

safety:
  max_actions_per_run: 10
  reconcile_hours: 12

notifications:
  email: ops@example.com
  slack_webhook_parameter: /clamper/slack-webhook
"""


# --- defaults -----------------------------------------------------------


def test_the_minimal_config_defaults_to_dry_run() -> None:
    """The one default that matters most. A tool that can stop your
    infrastructure must not act on a config that did not ask it to."""
    config = loads(MINIMAL)
    assert config.mode == MODE_DRY_RUN
    assert config.dry_run is True


def test_the_minimal_config_defaults_the_thresholds() -> None:
    limit = loads(MINIMAL).limits[0]
    assert limit.warn_percent == 80
    assert limit.clamp_percent == 100
    assert limit.warn_amount == 400
    assert limit.clamp_amount == 500


def test_the_minimal_config_defaults_the_tags_and_cap() -> None:
    config = loads(MINIMAL)
    assert config.targets.managed_tag == "clamper:managed"
    assert config.targets.managed_tag_value == "true"
    assert config.targets.protect_tag == "clamper:protect"
    assert config.targets.include_untagged is False
    assert config.safety.max_actions_per_run == 50
    assert config.safety.reconcile_hours == 6


def test_omitting_types_enables_every_action() -> None:
    """Rather than none. A config that names no types means "all of them",
    and an *empty list* is an error, because those are different intents."""
    config = loads(MINIMAL)
    assert "ec2-instance" in config.targets.types
    assert set(config.targets.types) == load_registry().ids()


def test_the_full_config_round_trips() -> None:
    config = loads(FULL, source="/repo/clamper.yml")
    assert config.mode == MODE_ENFORCE
    assert config.dry_run is False
    assert config.stack_name == "clamper-prod"
    assert config.regions == ("us-east-1", "eu-west-2")
    assert config.source == "/repo/clamper.yml"
    assert [limit.id for limit in config.limits] == ["whole-account", "ml-team"]
    assert config.targets.types == ("ec2-instance", "rds-instance")
    assert config.safety.max_actions_per_run == 10
    assert config.notifications.email == "ops@example.com"


def test_a_limit_can_be_looked_up_by_id() -> None:
    config = loads(FULL)
    assert config.limit("ml-team") is not None
    assert config.limit("nonexistent") is None


def test_budget_names_are_derived_from_the_stack_and_limit() -> None:
    """Derived rather than configurable: the enforcer finds the budget by
    name knowing only the stack name, and a separate setting is one more
    thing that can disagree."""
    config = loads(FULL)
    assert config.limits[0].budget_name(config.stack_name) == "clamper-prod-whole-account"


# --- scope --------------------------------------------------------------


def test_a_tag_scope_becomes_a_budget_cost_filter() -> None:
    """`user:<key>$<value>` is the format AWS Budgets takes."""
    scope = loads(FULL).limits[1].scope
    assert scope.cost_filter == {"TagKeyValue": ["user:CostCentre$ml"]}
    assert scope.label == "tag CostCentre=ml"


def test_an_account_scope_has_no_cost_filter() -> None:
    scope = loads(FULL).limits[0].scope
    assert scope.cost_filter == {}
    assert scope.label == "whole account"


def test_a_tag_scope_without_a_tag_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "    scope:\n      kind: tag\n")
    assert "scope.tag" in str(excinfo.value)


def test_a_tag_scope_without_a_value_is_refused() -> None:
    """A key with no value scopes the budget to every value of that tag,
    which is almost never what someone means and reads like a filter."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "    scope:\n      kind: tag\n      tag: Team\n")
    assert "scope.value" in str(excinfo.value)


def test_a_tag_on_an_account_scope_is_refused() -> None:
    """Rather than ignored. A tag key sitting under `kind: account` reads as
    though the limit is scoped when it is not."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "    scope:\n      kind: account\n      tag: Team\n")
    assert "no meaning" in str(excinfo.value)


def test_a_tag_scope_value_can_be_empty_string() -> None:
    """An empty tag value is a real thing in AWS and a real cost filter."""
    config = loads(MINIMAL + '    scope:\n      kind: tag\n      tag: Team\n      value: ""\n')
    assert config.limits[0].scope.cost_filter == {"TagKeyValue": ["user:Team$"]}


# --- unknown keys -------------------------------------------------------


def test_an_unknown_top_level_key_is_an_error() -> None:
    """Not a warning. A misspelled key here is a setting somebody believes is
    in effect, and the one they most often believe is in effect is the one
    that stops a resource being touched."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "enforcement: yes\n")
    assert "enforcement" in str(excinfo.value)
    assert "errors rather than warnings" in str(excinfo.value)


def test_a_misspelled_protect_tag_is_an_error_with_a_suggestion() -> None:
    """The exact failure this strictness exists for: `protect_tags` silently
    ignored would mean a resource somebody protected being stopped."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  protect_tags: keepout\n")
    message = str(excinfo.value)
    assert "protect_tags" in message
    assert "did you mean `protect_tag`" in message


def test_a_hyphenated_key_is_suggested_too() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  managed-tag: a:b\n")
    assert "did you mean `managed_tag`" in str(excinfo.value)


@pytest.mark.parametrize(
    "section,key",
    [
        ("limits", "cap"),
        ("targets", "allow"),
        ("safety", "max"),
        ("notifications", "webhook"),
    ],
)
def test_unknown_keys_are_rejected_in_every_section(section: str, key: str) -> None:
    if section == "limits":
        text = MINIMAL + f"    {key}: 1\n"
    else:
        text = MINIMAL + f"{section}:\n  {key}: 1\n"
    with pytest.raises(ConfigError) as excinfo:
        loads(text)
    assert key in str(excinfo.value)


# --- mode and version ---------------------------------------------------


def test_a_missing_version_is_an_error() -> None:
    """Rather than an assumption. The schema decides what can be stopped."""
    with pytest.raises(ConfigError) as excinfo:
        loads("limits:\n  - id: a\n    amount_usd: 1\n")
    assert "version" in str(excinfo.value)


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "mode: enforce-hard\n")
    assert "dry-run, enforce" in str(excinfo.value)


def test_an_empty_file_is_refused() -> None:
    """There is no "run on the defaults" path. A tool that can stop your
    infrastructure should not run on an implied configuration."""
    with pytest.raises(ConfigError) as excinfo:
        loads("")
    assert "clamper init" in str(excinfo.value)


def test_a_list_document_is_refused() -> None:
    with pytest.raises(ConfigError):
        loads("- version: 1\n")


def test_invalid_yaml_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads("version: 1\nlimits: [unclosed\n")
    assert "not valid YAML" in str(excinfo.value)


# --- limits -------------------------------------------------------------


def test_no_limits_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads("version: 1\nlimits: []\n")
    assert "nothing to enforce" in str(excinfo.value)


def test_a_missing_limits_key_is_refused() -> None:
    with pytest.raises(ConfigError):
        loads("version: 1\n")


def test_a_duplicate_limit_id_is_refused() -> None:
    """Ids name a budget, so a duplicate would mean two limits fighting over
    one budget in AWS."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "  - id: whole-account\n    amount_usd: 100\n")
    assert "share the id" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["Whole_Account", "-leading", "a" * 64, "", "has space"])
def test_a_bad_limit_id_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(f"version: 1\nlimits:\n  - id: {bad!r}\n    amount_usd: 1\n")


@pytest.mark.parametrize("bad", ["0", "-5", "nonsense", "true"])
def test_a_bad_amount_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(f"version: 1\nlimits:\n  - id: a\n    amount_usd: {bad}\n")


def test_a_warn_above_the_clamp_is_refused() -> None:
    """A warning that fires after the action is not a warning."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "    warn_percent: 95\n    clamp_percent: 90\n")
    assert "has to come first" in str(excinfo.value)


def test_a_warn_equal_to_the_clamp_is_allowed() -> None:
    """Unusual, but a coherent policy: notify and act at the same moment."""
    config = loads(MINIMAL + "    warn_percent: 100\n    clamp_percent: 100\n")
    assert config.limits[0].warn_percent == 100


def test_a_clamp_percent_above_100_is_allowed() -> None:
    """ "Clamp at 110% of budget" is a real policy."""
    assert loads(MINIMAL + "    clamp_percent: 110\n").limits[0].clamp_percent == 110


def test_a_four_figure_percent_is_refused() -> None:
    """Almost always an amount typed into a percentage field."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "    clamp_percent: 5000\n")
    assert "percentage of the limit, not an amount" in str(excinfo.value)


def test_an_unknown_notification_type_is_refused() -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + "    notification_type: guess\n")


# --- targets ------------------------------------------------------------


def test_an_unknown_type_is_refused_with_the_available_list() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  types: [ec2-terminate]\n")
    message = str(excinfo.value)
    assert "ec2-terminate" in message
    assert "ec2-instance" in message


def test_an_empty_types_list_is_refused() -> None:
    """It disables every action, which is almost certainly not what someone
    writing `types: []` meant. Removing the key is how you say "all"."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  types: []\n")
    assert "disables every action" in str(excinfo.value)


def test_a_string_where_a_list_belongs_is_refused_helpfully() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  types: ec2-instance\n")
    assert "must be a list" in str(excinfo.value)


def test_the_managed_and_protect_tags_cannot_be_the_same() -> None:
    """One enrols a resource and the other exempts it."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  managed_tag: x:y\n  protect_tag: x:y\n")
    assert "cannot be the same key" in str(excinfo.value)


def test_an_empty_protect_tag_is_refused() -> None:
    """An empty key matches nothing, which for protect_tag means nothing is
    protected — a disabled safety check that looks like a configured one."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + 'targets:\n  protect_tag: ""\n')
    assert "nothing is protected" in str(excinfo.value)


def test_an_invalid_tag_key_is_refused() -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + 'targets:\n  managed_tag: "bad!tag$key"\n')


def test_a_selector_names_resources_explicitly() -> None:
    config = loads(FULL)
    assert config.targets.ids_for("ec2-instance") == {"i-0123456789abcdef0"}
    assert config.targets.ids_for("rds-instance") == frozenset()


def test_an_empty_selector_is_refused() -> None:
    """It selects nothing while reading as though it selects everything of
    that type."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  selectors:\n    - type: ec2-instance\n      ids: []\n")
    assert "at least one resource" in str(excinfo.value)


def test_a_selector_for_an_unknown_type_is_refused() -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + "targets:\n  selectors:\n    - type: nope\n      ids: [x]\n")


# --- the untagged acknowledgement ---------------------------------------


def test_include_untagged_without_the_acknowledgement_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "targets:\n  include_untagged: true\n")
    message = str(excinfo.value)
    assert UNTAGGED_ACKNOWLEDGEMENT in message
    assert "stop everything you can find" in message


def test_a_near_miss_acknowledgement_is_refused() -> None:
    """Exactly that sentence. A paraphrase is somebody who did not read it."""
    text = MINIMAL + (
        "targets:\n"
        "  include_untagged: true\n"
        '  untagged_acknowledgement: "I understand this can stop resources nobody tagged."\n'
    )
    with pytest.raises(ConfigError):
        loads(text)


def test_the_exact_acknowledgement_is_accepted() -> None:
    text = MINIMAL + (
        f"targets:\n  include_untagged: true\n"
        f'  untagged_acknowledgement: "{UNTAGGED_ACKNOWLEDGEMENT}"\n'
    )
    assert loads(text).targets.include_untagged is True


def test_an_acknowledgement_without_include_untagged_is_refused() -> None:
    """So the file never reads as though untagged resources are in scope when
    they are not."""
    text = MINIMAL + f'targets:\n  untagged_acknowledgement: "{UNTAGGED_ACKNOWLEDGEMENT}"\n'
    with pytest.raises(ConfigError) as excinfo:
        loads(text)
    assert "Remove one of them" in str(excinfo.value)


# --- safety -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-1", "nonsense"])
def test_a_bad_cap_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + f"safety:\n  max_actions_per_run: {bad}\n")


def test_a_reconcile_interval_over_a_day_is_refused() -> None:
    """Budget data refreshes up to three times a day. Anything above 24 hours
    means a breach can sit unenforced for longer than a day."""
    with pytest.raises(ConfigError) as excinfo:
        loads(MINIMAL + "safety:\n  reconcile_hours: 48\n")
    assert "longer than a day" in str(excinfo.value)


def test_a_large_cap_is_allowed_because_it_is_the_users_call() -> None:
    assert loads(MINIMAL + "safety:\n  max_actions_per_run: 5000\n").safety.max_actions_per_run


# --- notifications ------------------------------------------------------


@pytest.mark.parametrize("bad", ["notanemail", "a@b", "a b@c.com", "a@b.com,c@d.com"])
def test_a_bad_email_is_refused(bad: str) -> None:
    """Caught here because the failure otherwise is an SNS subscription that
    is never confirmed and a clamp nobody hears about."""
    with pytest.raises(ConfigError):
        loads(MINIMAL + f"notifications:\n  email: {bad!r}\n")


def test_a_webhook_url_in_the_parameter_field_is_refused() -> None:
    """With the command to do it properly. A webhook URL is a credential and
    this field is the one place somebody will try to paste one."""
    with pytest.raises(ConfigError) as excinfo:
        loads(
            MINIMAL
            + "notifications:\n"
            + "  slack_webhook_parameter: https://hooks.slack.com/services/T/B/x\n"
        )
    assert "SecureString" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["clamper/hook", "/clamper/../other/secret", "/a//b"])
def test_a_bad_parameter_name_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + f"notifications:\n  slack_webhook_parameter: {bad!r}\n")


def test_a_valid_parameter_name_is_accepted() -> None:
    config = loads(MINIMAL + "notifications:\n  slack_webhook_parameter: /clamper/slack-webhook\n")
    assert config.notifications.slack_webhook_parameter == "/clamper/slack-webhook"


# --- regions and stack name ---------------------------------------------


@pytest.mark.parametrize("bad", ["useast1", "US-EAST-1", "us-east", "eu_west_1"])
def test_a_bad_region_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + f"regions: [{bad}]\n")


@pytest.mark.parametrize("good", ["us-east-1", "eu-west-2", "ap-southeast-4", "us-gov-west-1"])
def test_real_region_names_are_accepted(good: str) -> None:
    assert loads(MINIMAL + f"regions: [{good}]\n").regions == (good,)


@pytest.mark.parametrize("bad", ["1clamper", "clamper_prod", "clamper.prod", ""])
def test_a_bad_stack_name_is_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        loads(MINIMAL + f"stack_name: {bad!r}\n")


# --- finding the file ---------------------------------------------------


def test_find_config_walks_up(tmp_path: Path) -> None:
    (tmp_path / "clamper.yml").write_text(MINIMAL, encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_config(deep) == tmp_path / "clamper.yml"


def test_find_config_accepts_the_yaml_spelling(tmp_path: Path) -> None:
    (tmp_path / "clamper.yaml").write_text(MINIMAL, encoding="utf-8")
    assert find_config(tmp_path) == tmp_path / "clamper.yaml"


def test_find_config_returns_none_when_there_is_none(tmp_path: Path) -> None:
    assert find_config(tmp_path / "nothing-here") is None


def test_load_reports_the_path_in_every_error(tmp_path: Path) -> None:
    path = tmp_path / "clamper.yml"
    path.write_text("version: 1\nnonsense: 1\nlimits: []\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load(path)
    assert str(path) in str(excinfo.value)


def test_load_of_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load(tmp_path / "absent.yml")
    assert "Could not read" in str(excinfo.value)
