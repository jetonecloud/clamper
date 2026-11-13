# Contributing to Clamper

Thanks for considering a contribution. Clamper is Apache-2.0 and built in the
open — see [`README.md`](README.md) for what it does and
[`docs/design.md`](docs/design.md) for how it is put together and why.

Clamper can stop somebody's infrastructure. That changes what a contribution
has to prove, and most of this file is about how.

## Ground rules

1. **One write path.** Every call that changes a customer's resource goes
   through `Actuator.invoke` in [`core/actuator.py`](src/clamper/core/actuator.py),
   which takes the boto3 method name from [`actions.yml`](src/clamper/data/actions.yml)
   rather than from its caller. No boto3 method name may appear in the Python
   source at all. [`tests/test_safety.py`](tests/test_safety.py) enforces this
   structurally; if you find yourself wanting to call a write method anywhere
   else, the design needs discussing first.
2. **Nothing that terminates or deletes.** The registry refuses to load a row
   whose method or IAM action starts with a destructive verb, and the safety
   test names thirty-odd destructive boto3 operations one at a time. The one
   exception, `lambda:DeleteFunctionConcurrency`, is a single named string
   with its reason beside it. It is not a precedent.
3. **Reversible, from what the ledger recorded.** An action is in Clamper
   only if `clamper release` can put it back from the recorded previous
   state alone. "Rebuild it and hope" is not a release.
4. **Recorded before acting.** `ledger.record()` before `actuator.invoke()`,
   always. A clamp that cannot be recorded must not happen.
5. **A failure is never counted as done.** Counts come from ledger status,
   and only a performed call settles to `clamped`. A dry run is
   `would-clamp`, an unknown outcome is `pending`, and both are listed under
   NOT CLAMPED. Reporting "stopped 14 resources" when thirteen stopped is a
   security issue, not a cosmetic one.
6. **Something that could not be checked is never reported as clean.** A
   region that could not be read, a budget that could not be described, a
   ledger that could not be queried: each is an error in the report and exit
   code 2, never an empty list.
7. **No number you cannot defend.** A price needs a `basis` sentence someone
   can check. If you cannot write one, the figure is `None` and the report
   says "price unknown".
8. **Plain language.** Reports and notifications are read at 3am by somebody
   who did not write the config. Say what happened and what to do.
9. **License hygiene.** Don't copy GPL/AGPL code into this repo. If you port
   logic from another open-source project, confirm the licence is compatible
   and add it to [`NOTICE`](NOTICE).

## Development setup

```sh
git clone https://github.com/jetonecloud/clamper.git
cd clamper
uv sync --all-extras --dev
uv run pytest -q
```

- Python 3.11+, managed with `uv` and `pyproject.toml`.
- Lint and format with **Ruff**: `uv run ruff check . && uv run ruff format .`
- Type-check with **mypy** in strict mode: `uv run mypy`
- Tests with **pytest**. No test touches a real AWS account; `conftest.py`
  clears every credential variable so a developer's profile cannot leak in.
  - **moto** where state matters — the ledger, and every clamp/release round
    trip, so the state after a clamp is a real transition rather than a
    fixture somebody wrote to match.
  - **botocore's `Stubber`** where it does not, and for CloudFormation, S3,
    SNS and Cost Explorer. Stubber validates every request against the real
    service model, so a wrong parameter name fails in CI rather than in
    somebody's account.
- `uv run clamper demo` runs the real plan and reconcile against fixtures,
  with no AWS at all.
- `uv run clamper deploy --template-only --config examples/clamper.yml --out /tmp/clamper && cfn-lint /tmp/clamper/*.yaml`
  checks the generated templates. CI does this too.

## The client-naming convention

The safety tests find "a call on a boto3 client" statically, and they can
only do that because the package keeps one convention:

- **A boto3 client is obtained with `clients.get("<service>")`, with the
  service name as a string literal**, and
- **bound to a local called `client` or one ending in `_client`.**

```python
client = clients.get("budgets")  # yes
response = client.describe_budget(...)

ledger_client = clients.get("dynamodb")  # yes

c = clients.get("ec2")  # no: invisible to the check
clients.get(service_name).describe_instances()  # no: which service?
```

With that, `tests/test_safety.py` can hold every call outside the actuator to
`describe_`/`get_`/`list_` (and `query`, `scan`, the paginator helpers), and
can tell a call on `ec2` — a service somebody pays for, where there are no
exceptions at all — from one on `dynamodb`. Break the convention and the
check silently stops seeing your call, which is why a reviewer will ask you
to rename it.

Clients are built in [`core/session.py`](src/clamper/core/session.py) and
nowhere else, and boto3 is imported only there.

### Writes to Clamper's own infrastructure

Clamper has to write to things that are its own: the ledger table, its
notifications topic, its two stacks and its artifact bucket. Those are pinned,
per module and per method, in `ALLOWED_OWN_WRITES` in
[`tests/test_safety.py`](tests/test_safety.py), each with the reason:

| Module | Methods | Why |
|---|---|---|
| `core/ledger.py` | `put_item` | The ledger. No update, and deliberately no delete: it is never pruned. |
| `enforcer/notify.py` | `publish` | The notification, to Clamper's own topic. |
| `deploy.py` | `create_stack`, `update_stack`, `put_object` | `clamper deploy`: its own two stacks, and the zip into its own bucket. |

Adding to that table is a diff to a test, which is a diff somebody reviews.
Say in the PR why the write is to Clamper's own infrastructure and not to a
customer's.

## Adding an action

An action is a resource type Clamper can clamp. It is the most useful kind
of contribution and the one held to the highest bar.

1. **A row in [`actions.yml`](src/clamper/data/actions.yml).** The id, the
   boto3 client, the identifier shape, the ARN pattern, the tag condition key
   *that service actually uses* (Auto Scaling's is `autoscaling:ResourceTag/`,
   not `aws:ResourceTag/` — a wrong key is not an error, it just never
   matches), the discovery reads, the fields to record, the clamp and release
   calls with their parameter names, and notes on what the clamp does to a
   person. Say how you established the tag condition key in the PR.
2. **A handler in [`core/resources/`](src/clamper/core/resources/)**:
   `discover`, `live_state`, `clamp_params`, `applied_state` and
   `release_params`. Discovery returns every resource with a reason for the
   ones it cannot clamp; it never silently drops one.
3. **A moto round trip** in `tests/test_resource_<type>.py` using
   [`tests/roundtrip.py`](tests/roundtrip.py): discover, record, clamp,
   settle, read the live state, release, and assert the resource is back as
   it was. Plus the cases where it should *not* clamp.
4. **No change to the actuator.** A row that needs a special case in
   `actuator.py` is a row whose design is not finished.
5. Update the pinned method set in `test_the_allowlist_is_pinned`, regenerate
   the IAM examples with `clamper policy` (the test that checks them prints
   the command), and add the type to [`docs/actions.md`](docs/actions.md).

Open an issue first. The questions that decide whether an action belongs are
"can it be released from what is recorded?" and "what does it do to somebody
at 3am?", and they are better answered before the code.

## Adding an output format

Add a module under [`core/report/`](src/clamper/core/report/) with a
`render(report) -> str` and register it in `FORMATS`, `EXTENSIONS` and the
renderer map. A renderer is a **pure function of the `Report`**: no I/O, no
clock. Free text — names, descriptions, error messages — is untrusted and has
to be escaped for the format; the terminal renderer strips control characters
and ANSI, the Markdown one escapes pipes and angle brackets.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):
`<type>: <summary>`, then a body explaining *why*. Common types: `feat`,
`fix`, `docs`, `test`, `ci`, `build`, `chore`, `refactor`.

Write the body for someone reading `git log` in a year, and say what you
tried that didn't work — that's usually the valuable part.

```
fix: treat a terminated or unknown EC2 instance as gone, not as an error

EC2 answers DescribeInstances for an unknown id with InvalidInstanceID.NotFound
rather than an empty list, so live_state raised where every other handler
returns None. And a terminated instance stays visible for about an hour in a
state that is neither stopped nor running.

Both now return None. For a release that is the difference between "could
not read the resource" - which a person would retry - and "the resource is
gone, there is nothing to restore", which is the true answer.
```

## Pull requests

1. Open an issue first for anything beyond a small fix.
2. Keep PRs focused — one action, one fix.
3. Include tests, including a case where the change should *not* act. CI
   (lint, mypy, pytest on three Python versions, the safety job, the demo
   from a clean install, cfn-lint on the templates) must pass.
4. Say what your change can write, and to what, so a reviewer can check it.
   "Nothing new" is a good answer.
5. By submitting a PR you agree your contribution is licensed under
   Apache-2.0.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Bugs vs. security issues

- Functional bugs: open a GitHub issue.
- Anything that could make Clamper stop something it should not, fail to
  release something it stopped, or report a clamp that did not happen as
  done: follow [`SECURITY.md`](SECURITY.md) instead. Please don't open a
  public issue for those.
