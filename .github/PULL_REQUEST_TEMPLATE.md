## What this changes

## Why

What problem this solves. If you tried something that didn't work first,
that's usually the valuable part of the description.

## What it can write, and to what

So a reviewer can check it. "Nothing new" is a perfectly good answer.

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest -q`
- [ ] `uv run clamper demo` still runs
- [ ] Tests added for the new behaviour, including a case where it should
      *not* act
- [ ] Every write to a customer's resource still goes through
      `Actuator.invoke`; no boto3 method name appears in the source
- [ ] Any new client call follows the naming convention in CONTRIBUTING.md,
      and any new write to Clamper's own infrastructure is in
      `ALLOWED_OWN_WRITES` with a reason
- [ ] A failure, or something that could not be read, still cannot be
      reported as done or as clean
- [ ] If `actions.yml` changed: the round-trip test passes, the pinned
      method set and `iam/` examples are updated, and `docs/actions.md` says
      what the clamp does to a person
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

## Notes for the reviewer
