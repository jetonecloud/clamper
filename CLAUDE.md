# Working on this repo with Claude Code

Project conventions live in [`CONTRIBUTING.md`](CONTRIBUTING.md), including
the client-naming convention the safety tests rely on. Read it first — this
file only covers things specific to working here with an agent.

## Commits

- Follow [Conventional Commits](https://www.conventionalcommits.org/), as
  `CONTRIBUTING.md` describes: `<type>: <summary>`, body explaining why.
- **Do not add `Co-Authored-By: Claude` or any other agent attribution
  trailer**, and don't mention Claude or Claude Code in commit messages.
  The commit author is the person running the tool.
- The same goes for pull request descriptions: no "generated with" footer.
- Prefer several small, structured commits over one large one when a change
  has distinct parts.
- One session at a time on this repo. Two sessions committing to the same
  branch overwrite each other's work.

## Things that are easy to get wrong here

- **No boto3 method name in the Python source.** Writes go through
  `Actuator.invoke` with an action id from `actions.yml`. A non-read client
  call anywhere else fails `tests/test_safety.py`; a write to Clamper's own
  infrastructure belongs in `ALLOWED_OWN_WRITES` with a reason, and a write
  to a customer's resource does not belong anywhere but the actuator.
- **Nothing under `core/` or `enforcer/` may import `typer`, `click` or
  `rich`.** Neither is in the Lambda zip, so the import fails at cold start.
- **The IAM examples in `iam/` are generated.** If `test_policy.py` says one
  is stale, regenerate it with the command the failure prints rather than
  editing the JSON.

## Before you call something done

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
uv run clamper demo
```

All four have to pass. If a test fails, say so with the output rather than
describing the change as finished.
