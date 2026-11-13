# Clamper

Clamper makes an AWS budget enforceable. When spend crosses a limit you set,
it stops the specific resources you allowed it to stop, tells you exactly
what it stopped and what it could not, and puts everything back with one
command.

AWS has no hard spending cap. AWS Budgets sends an email when you cross a
number; it does not stop the GPU instance somebody forgot, the Auto Scaling
group that scaled to 200, or the Lambda in a retry loop. Clamper does, within
limits that are worth reading before anything else.

## What it cannot do

Read this first. Each of these is a property of AWS, not a missing feature.

- **It works in hours, not cents.** Clamper decides from AWS Budgets, and
  AWS says Budgets data "is updated up to three times a day". A clamp happens
  within hours of the billing data showing the breach, not at the exact
  cent. Anybody promising a cent-accurate hard cap on AWS is selling you
  something.
- **Money already spent stays spent.** A clamp stops *future* charges from
  the resources it stops. The hours already accrued are billed.
- **Some charges cannot be stopped by stopping anything.** Savings Plans and
  Reserved Instances bill whether or not you use them. Marketplace
  subscriptions and AWS Support bill on their own schedule. Storage — EBS
  volumes, RDS storage, snapshots, S3 — keeps billing while compute is
  stopped. Data transfer already in flight is already incurred.
- **A clamp is an outage.** That is what it is for, and it is reversible and
  recorded, but it is not free. A stopped database drops its connections, and
  starting one again can take minutes to hours.
- **Reversible is not lossless.** An EC2 instance with instance-store volumes
  loses that data when it stops. An in-memory cache is gone. A stopped
  instance may come back with a different public IPv4 address. Clamper only
  takes actions whose *configuration* it can restore; it cannot restore what
  a resource was holding in RAM or on ephemeral disk.

## Why trust something that can stop your infrastructure

Unlike Clamper's sibling tools, this one has to write: a limit that cannot
stop anything is just another alert. So the trust comes from four properties
instead, each of them checked by tests rather than promised in prose.

1. **Opt-in.** A resource is a candidate only if you tagged it
   `clamper:managed=true` or named it in your config. Anything tagged
   `clamper:protect` is never touched, and nothing in the config can override
   that. Including untagged resources takes a key *and* a sentence you have
   to type out.
2. **Declared up front.** Every write Clamper can make is one row in
   [`actions.yml`](src/clamper/data/actions.yml) — twelve calls, each with its
   inverse. The IAM policy is generated from the same file, and a row with a
   terminate or delete verb makes the package refuse to load.
3. **Recorded before acting.** What a resource was is written to a DynamoDB
   ledger *before* the call that changes it. A crash between the two leaves
   an entry that says "outcome unknown", never one that says "done".
4. **Reversible with one command.** `clamper release <clamp-id>` restores
   exactly what the ledger recorded, and the notification you get tells you
   the command to paste.

And it starts in `dry-run`, where it decides, records and notifies and stops
nothing. Moving to `enforce` is an edit you make on purpose.

## Getting started

Installed from the GitHub release, pinned to a tag:

```sh
pipx install git+https://github.com/jetonecloud/clamper@v0.1.0
clamper demo     # a plan and a clamp run against a made-up account, no AWS
```

Then, in the repository you deploy from:

```sh
clamper init --email you@example.com   # writes clamper.yml, in dry-run
clamper plan                            # what would be stopped. Only reads.
clamper deploy                          # the budgets, the enforcer, its ledger
clamper doctor                          # is the email confirmed? is the tag active?
```

`uv`, a plain virtualenv, the release wheel and Docker all work too — see
[`docs/installation.md`](docs/installation.md). Credentials come from the
standard chain (`AWS_PROFILE`, SSO, an instance role, `--role-arn`), never
from a flag you paste a key into.

AWS emails a confirmation link to the address in your config. **Nothing is
delivered until somebody clicks it**, and `clamper doctor` fails until they
have.

## What it can stop

| Resource | Clamp | Release |
|---|---|---|
| EC2 instances | stop | start, only the ones Clamper stopped |
| Auto Scaling groups | min and desired capacity to 0 | the recorded min, max and desired |
| ECS services | desired count to 0 | the recorded count |
| RDS instances | stop | start |
| Aurora clusters | stop | start |
| Lambda functions | reserved concurrency to 0 | the recorded value, or no reservation if there was none |
| SageMaker notebook instances | stop | start |

Every one is reversible and none deletes anything. [`docs/actions.md`](docs/actions.md)
has the details of each, including the ones AWS undoes by itself (RDS
restarts a stopped instance after seven days; Clamper notices, stops it
again, and tells you).

Deliberately **not** in v1: SageMaker endpoints (the only way to stop their
cost is to delete them), EKS node groups (scaling to zero ignores pod
disruption budgets), anything that terminates or deletes, and anything
data-plane. `clamper plan` still lists the SageMaker endpoints it finds, with
their price, so the biggest number stays on the page.

## How it works

```
Budget --> SNS alerts --+
                        v
EventBridge (6h) ---> enforcer Lambda --> reads DescribeBudget
                        |                 ok | warn | clamp
                        v
              ledger.record()  then  stop  then  ledger.settle()
                        |
                        v
              SNS notifications --> your email
```

Two triggers, one code path. Budgets publishes to an alerts topic when a
threshold is crossed; an EventBridge schedule runs the same reconcile every
six hours, because AWS sends an actual-spend alert only **once per budget per
period** and one missed delivery would otherwise mean no clamp until next
month. The alert's message body is never parsed: the enforcer reads the
spend from Budgets itself, so a spoofed or replayed message cannot invent a
breach.

Above `max_actions_per_run` targets (default 50), Clamper clamps **nothing**
and tells you, rather than stopping an arbitrary first fifty. A plan far
bigger than usual is more likely a stray tag than a real need.

[`docs/design.md`](docs/design.md) has the architecture and every decision
behind it, quoting the AWS documentation each one rests on.

## Commands

| Command | Touches |
|---|---|
| `version`, `init`, `validate`, `demo`, `policy`, `deploy --template-only` | Nothing in AWS |
| `plan`, `status`, `ledger`, `doctor`, `simulate` | Reads only |
| `deploy` | Clamper's own two stacks and its own bucket |
| `clamp`, `release` | Your tagged resources, through the one write path |

```sh
clamper plan --format markdown >> "$GITHUB_STEP_SUMMARY"
clamper simulate --assume-breach        # the real enforcer, dry-run forced, real reads
clamper status                          # limits, and what is stopped right now
clamper ledger                          # every clamp run; `clamper ledger <id>` for one
clamper release 20261113t153000z-a1b2c3 # put back what that run stopped
clamper release --all --dry-run         # what releasing everything would do
```

Reports come in three formats — terminal (plain ASCII, survives a CI log and
a paste into Slack), Markdown, and JSON with a versioned schema documented in
[`docs/json-schema.md`](docs/json-schema.md). Reports go to stdout and
everything else to stderr, so `clamper plan --format json > plan.json` works.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing to do |
| 1 | A limit is breached, or something is clamped |
| 2 | Something could not be checked or done, or the tool failed |

2 beats 1. A resource Clamper failed to stop, or a clamp whose outcome is
unknown, is a hole in enforcement and is the more urgent of the two.

## What it costs to run

Nothing, in the common case: one budget, Lambda invocations four times a day,
a few DynamoDB writes per clamp and a few emails all sit inside AWS's free
tiers. No budget actions (which cost $0.10 a day each after the first two),
no Cost Explorer queries ($0.01 each), nothing always-on. The arithmetic is
in [`docs/design.md`](docs/design.md#what-clamper-costs-to-run).

## Permissions

Three policies, in [`iam/`](iam/README.md): the enforcer's, generated from
`actions.yml` and scoped to your managed tag; the operator's, for a person
running `plan`, `status`, `release` and `clamp`; and the deploy policy, which
is broad because creating a stack is, and which you need once. If you would
rather not hold it, `clamper deploy --template-only` writes both templates
and the zip to a directory with no AWS calls, and you can deploy them from
the console.

[`docs/threat-model.md`](docs/threat-model.md) covers what Clamper can be
made to do, who has to be trusted, and what is not protected.
`clamper.yml` decides what gets stopped: treat it as production
infrastructure, require review on it, and never run `clamper deploy` from a
workflow a pull request can trigger.

## Contributing

Welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Security issues go to
[`SECURITY.md`](SECURITY.md), not to a public issue.

## Releases

Tagged releases are on the
[releases page](https://github.com/jetonecloud/clamper/releases), each with
a wheel, an sdist and a `checksums.txt`. There is no PyPI package: pin a tag
and you know exactly what you are running.

## License

Apache License 2.0, see [`LICENSE`](LICENSE). Attribution for third-party
content is in [`NOTICE`](NOTICE).

## Want help with your AWS bill?

That's what we do at [Jet1](https://jetonecloud.com): deploys, AWS
infrastructure, cost work, and on-call, with an SLA, and you own everything we
build. [Book a 20-minute call](https://jetonecloud.com/book).
