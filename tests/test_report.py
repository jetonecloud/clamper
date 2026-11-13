"""The renderers: honest counts, safe text, and a stable JSON shape."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_notify import LIMIT, line, report

from clamper.core.report import FORMATS, json_out, markdown, model, render, terminal

HOSTILE = "evil\x1b[2J\x1b[31m|`<script>`|\r\nFAKE LINE\x07"


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_renders(fmt: str) -> None:
    assert render(report(), fmt)


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="html"):
        render(report(), "html")


# --- honesty ------------------------------------------------------------


def test_pending_is_never_counted_as_done() -> None:
    """The threat model's second headline risk. A crash between record and
    settle must not read as a clamp anywhere."""
    pending = report(acted=(), not_acted=(line(model.PENDING, detail="outcome unknown"),))
    assert pending.stopped_count == 0
    assert pending.exit_code == model.EXIT_ATTENTION
    text = terminal.render(pending)
    assert "NOT CLAMPED" in text
    assert "[pending]" in text
    data = json_out.to_dict(pending)
    assert data["summary"]["stopped"] == 0
    assert data["summary"]["needs_attention"] == 1


def test_a_dry_run_is_never_counted_as_stopped() -> None:
    dry = report(mode="dry-run", acted=(line(model.WOULD_CLAMP),), not_acted=())
    assert dry.stopped_count == 0
    assert json_out.to_dict(dry)["summary"]["stopped"] == 0
    assert "WOULD CLAMP (dry-run)" in terminal.render(dry)
    assert dry.release_command is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"acted": (), "not_acted": (), "limits": ()}, model.EXIT_OK),
        ({"not_acted": ()}, model.EXIT_BREACH),
        ({}, model.EXIT_ATTENTION),  # a failed line beats the clamp
        ({"acted": (), "not_acted": (), "errors": ("could not read",)}, model.EXIT_ATTENTION),
        ({"acted": (), "not_acted": (), "over_cap": True}, model.EXIT_BREACH),
    ],
)
def test_exit_codes(changes: dict[str, Any], code: int) -> None:
    assert report(**changes).exit_code == code


def test_lines_are_ordered_most_expensive_first() -> None:
    cheap = model.Line(
        "clamped", "ec2-instance", "EC2 instance", "i-a", "us-east-1", hourly_usd=0.1
    )
    dear = model.Line("clamped", "ec2-instance", "EC2 instance", "i-b", "us-east-1", hourly_usd=9.0)
    unknown = model.Line("clamped", "ecs-service", "ECS service", "p/a", "us-east-1")
    assert [item.target_id for item in model.ordered([unknown, cheap, dear])] == [
        "i-b",
        "i-a",
        "p/a",
    ]


# --- safe text ----------------------------------------------------------


def test_the_terminal_is_ascii_with_no_escape_sequences() -> None:
    text = terminal.render(report(not_acted=(line(model.FAILED, detail=HOSTILE),)))
    assert all(32 <= ord(char) < 127 or char == "\n" for char in text)
    assert "\x1b" not in text
    assert "\nFAKE LINE" not in text


def test_markdown_escapes_table_breaking_characters() -> None:
    text = markdown.render(report(not_acted=(line(model.FAILED, detail=HOSTILE),)))
    row = next(item for item in text.splitlines() if "evil" in item)
    # The row still has exactly its seven pipes: nothing in the detail broke out.
    assert row.count("|") - row.count("\\|") == 7
    assert "<script>" not in text
    assert "\x1b" not in text


def test_json_round_trips_hostile_text() -> None:
    data = json.loads(json_out.render(report(not_acted=(line(model.FAILED, detail=HOSTILE),))))
    assert data["not_acted"][0]["detail"] == HOSTILE


# --- the JSON contract --------------------------------------------------

TOP_LEVEL = {
    "schema_version",
    "tool",
    "tool_version",
    "kind",
    "generated_at",
    "stack_name",
    "mode",
    "dry_run",
    "actor",
    "account",
    "regions",
    "exit_code",
    "summary",
    "clamp_id",
    "release_command",
    "limits",
    "acted",
    "not_acted",
    "active",
    "protected",
    "unsupported",
    "errors",
    "notes",
}


def test_every_top_level_key_is_always_present() -> None:
    """null rather than absent, so a missing key and a null key are not two
    different questions."""
    empty = model.Report(kind=model.KIND_PLAN, stack_name="clamper", mode="dry-run")
    assert set(json_out.to_dict(empty)) == TOP_LEVEL
    assert set(json_out.to_dict(report())) == TOP_LEVEL


def test_the_schema_is_documented() -> None:
    """docs/json-schema.md lists every top-level key, so the doc cannot fall
    behind the code without this failing."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "json-schema.md").read_text(
        encoding="utf-8"
    )
    missing = [key for key in TOP_LEVEL if f"`{key}`" not in doc]
    assert missing == []
    assert f"`{json_out.SCHEMA_VERSION}`" in doc


def test_limit_lines_carry_the_decision() -> None:
    (limit,) = json_out.to_dict(report())["limits"]
    assert limit["level"] == "clamp"
    assert limit["actual_usd"] == 150.0
    assert limit["forecast_usd"] is None
    assert LIMIT.id == limit["id"]
