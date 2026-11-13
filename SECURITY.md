# Security Policy

Clamper can stop your infrastructure. That is its job, and it is also the
reason to read this before deploying it anywhere you care about.

[`docs/threat-model.md`](docs/threat-model.md) is the full reasoning: who has
to be trusted, the attacker scenarios, and what is not protected. This file is
the policy and the summary.

## What is guaranteed by construction

- **One write path, declared up front.** Every call that changes one of your
  resources goes through `Actuator.invoke` in
  [`core/actuator.py`](src/clamper/core/actuator.py), which takes the boto3
  method from [`actions.yml`](src/clamper/data/actions.yml) by action id and
  never from its caller. Twelve methods in all: seven clamps and their
  inverses, some of which share a method.
  [`tests/test_safety.py`](tests/test_safety.py) asserts that no boto3 method
  name appears anywhere in the Python source, that dynamic dispatch happens
  only in the actuator, that every other client call is a read or one of a
  handful of pinned writes to Clamper's own infrastructure, and that the
  method set is exactly those twelve.
- **Nothing that terminates or deletes.** A row in `actions.yml` with a
  terminate, delete or other destructive verb makes the package fail to
  import. The single exception, `lambda:DeleteFunctionConcurrency`, removes a
  concurrency *setting* and is named with its reason. A test separately
  names thirty-odd destructive boto3 operations and asserts none appears
  anywhere in the package.
- **Opt-in only.** A resource is a candidate only with the managed tag or an
  explicit selector. `clamper:protect` is checked last and nothing — no
  config key, no `--force`, no `--yes` — overrides it. Including untagged
  resources needs a second key containing a sentence.
- **Recorded before acting.** The previous state is written to the ledger
  before the call. A crash in between leaves `pending`, which is reported as
  "outcome unknown" and exit 2, never as done.
- **Reversible with one command.** `clamper release` restores what the ledger
  recorded, refuses a resource somebody changed by hand since (unless
  `--force`), and validates every recorded identifier and value before
  building a call from it — the ledger is untrusted input.
- **Dry-run by default.** A fresh config stops nothing. The enforcer can
  clamp only in `mode: enforce`, and only on a breach it read from
  `DescribeBudget` itself: the SNS message that wakes it is never parsed.
- **It cannot clamp itself.** Three independent self-exclusion checks, each
  tested alone with the other two disabled.
- **Least privilege, generated.** The enforcer's IAM policy is generated from
  `actions.yml`, scopes every write to the managed tag with that service's
  own condition key, has no `iam:*`, `cloudformation:*` or `sts:*`, and has no
  `dynamodb:DeleteItem`. [`tests/test_policy.py`](tests/test_policy.py)
  refuses wildcard actions, destructive verbs and unconditioned writes on
  `Resource: "*"`, in the policy and in the template.
- **Credentials are never handled directly.** The standard SDK chain only.
  There is no flag, prompt or config key that accepts an access key. A Slack
  webhook URL lives in SSM as a `SecureString`; only its parameter name is
  ever in the config, the template or the ledger.
- **No telemetry.** Nothing is sent anywhere except the AWS calls the
  commands make and the notifications you configured.

## What is *not* guaranteed

The part worth reading twice.

- **Whoever controls `clamper.yml` controls what stops.** A config setting
  `amount_usd: 1` clamps everything you opted in on the next reconcile. That
  cannot be designed away — setting the limit is the product — so treat the
  file as production infrastructure, require review on it, and never run
  `clamper deploy` from a workflow a pull request can trigger.
- **Whoever can tag can opt a resource in, or remove its protection.** Use
  selectors rather than tags where `ec2:CreateTags` is widely held.
- **A clamp is an outage**, and reversible is not lossless: instance-store
  volumes, RAM and a notebook's local disk are lost on stop.
- **It works in hours, not cents**, and cannot stop money already spent or
  charges that do not come from a running resource.
- **The IAM tag conditions are the second layer, not the first.** Some of
  the condition keys were established from service documentation rather than
  AWS's authorization tables; a wrong one makes a permission narrower than
  intended, not wider. The runtime tag check is the guard that matters.
- **`clamper deploy` needs broad permissions**, including creating a named IAM
  role. `--template-only` lets you deploy from the console instead.

## Supported versions

Only the latest released version is supported with security fixes. Please
update before reporting an issue, to confirm it still reproduces.

## Verifying a release

Clamper is distributed through
[GitHub releases](https://github.com/jetonecloud/clamper/releases) rather
than PyPI, and installs pin a tag. Every release carries a `checksums.txt`
and a signed build provenance attestation:

```sh
gh release download v0.1.0 --repo jetonecloud/clamper
sha256sum --check --ignore-missing checksums.txt
gh attestation verify clamper-0.1.0-py3-none-any.whl --repo jetonecloud/clamper
gh attestation verify oci://ghcr.io/jetonecloud/clamper:0.1.0 --repo jetonecloud/clamper
```

The attestation ties the artifact to the workflow run and commit that built
it, so a wheel uploaded by hand — even by someone with write access — does not
verify. A git tag is mutable by anyone with write access; if that matters to
you, install the attested wheel rather than resolving a tag.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Email **security@jetonecloud.com** with:

- A description of the issue and its potential impact
- Steps to reproduce (a minimal repro is ideal)
- The Clamper version and platform

We aim to acknowledge within 2 business days and will keep you updated. We're
happy to credit reporters in release notes if you'd like.

**Treat these as critical and report them immediately:**

- Any mutating AWS call that does not go through `core/actuator.py`, or any
  way to make the actuator call a method that is not in `actions.yml`.
- A terminate or delete call reaching AWS from any code path.
- Any way to make Clamper clamp in `dry-run` mode.
- Any way to clamp a resource carrying the protect tag, one outside the
  configured scope and selection, or one of Clamper's own resources.
- Any way to exceed `max_actions_per_run` other than a person running
  `clamper clamp --yes`.
- A failed or unknown-outcome clamp reported as done, in any output format or
  notification.
- A clamp that `clamper release` cannot release given an intact ledger.
- Credentials or a Slack webhook URL written to disk, a log, a template
  parameter or the ledger.

## Scope

In scope: the `clamper` CLI, the enforcer Lambda and the templates it
generates, the published container image, and the generated IAM policies.

Out of scope: vulnerabilities in AWS or third-party dependencies with their
own disclosure process — please report those upstream, and let us know so we
can track the fix.
