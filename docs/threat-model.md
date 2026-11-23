# Threat model

What Clamper can and cannot be made to do, who has to be trusted, and the
places where the honest answer is "this is not protected".

[`SECURITY.md`](../SECURITY.md) is the policy and the reporting process. This
is the reasoning. [`design.md`](design.md) is the architecture these
mitigations live in.

## The thing that makes this different

Preflight and Crosswind cannot write. Their threat models are mostly about
*disclosure* — a report that leaks a secret, a plan file left in `/tmp`.

Clamper can stop your infrastructure. So the headline risk is not disclosure,
it is **an unintended clamp**: Clamper stopping something it should not have,
at a moment when it matters. A production database stopped during business
hours is a worse outcome than a leaked instance ID, and it is the outcome
every layer below is arranged against.

The second headline risk is the mirror image and is easy to forget:
**a clamp reported as done that did not happen**. Someone reads "stopped 14
resources", believes the spend has stopped, and goes back to bed. That is why
a failed call is never counted as success anywhere in this tool, and why
`pending` is a real state rather than an optimistic one.

## What Clamper handles

| Asset | Sensitivity |
|---|---|
| Permission to stop your infrastructure | **Highest.** This is the asset. |
| The ledger | What is currently stopped, and what it was before. A map of what to restore, and of what is currently off. |
| AWS credentials | Never touched directly; resolved by the SDK for the CLI, an execution role for the enforcer |
| `clamper.yml` | Decides what may be stopped |
| Spend figures | Your AWS bill, to the dollar |
| Notifications | Resource IDs and amounts, delivered by email |
| A Slack webhook URL, if configured | A credential. Kept in SSM `SecureString`, never in a template |

## Who has to be trusted

**Trusted:**

- Whoever can change `clamper.yml` in the repository you deploy from
- Whoever can tag resources in the account
- Whoever can update the CloudFormation stack
- The AWS credentials used for `clamper deploy`
- AWS Budgets' arithmetic

**Not trusted, and defended against:**

- The SNS message body that triggers the enforcer
- EventBridge event contents
- Resource names, tags and ARNs, as *data*
- The ledger's contents as a source of method names or ARNs to call
- Anything in `clamper.yml` that looks like a method, a verb or a path

**Out of scope:**

- An attacker who already holds credentials that can stop your resources
  directly. They do not need Clamper, and Clamper's existence does not help
  them.
- An attacker who can modify the Clamper installation or the deployed Lambda
  code.
- AWS itself.

## Attacker scenarios

### Someone commits a malicious `clamper.yml`

The config comes from the repository you deploy from, so it is
attacker-controlled anywhere a PR can change it. **And it decides what gets
stopped**, which makes this the most consequential untrusted input in the
tool.

| Attempt | Outcome |
|---|---|
| `amount_usd: 0.01` to force a permanent breach | **Possible, and the residual risk.** See below. |
| `mode: enforce` on a repo that was dry-run | **Possible.** Visible in the diff, and the deploy that applies it needs stack-update rights. |
| An action, method or ARN named in the config | **Rejected.** There is no config key that names a boto3 method, an IAM action or a resource ARN to call. Types are referenced by action **id** and the id is looked up in `actions.yml`; an unknown id is a load error. |
| `types: [ec2-terminate]` | **Rejected.** No such row exists and none can: the registry refuses to load a destructive verb. |
| `protect_tag: ""` to disable protection | **Rejected.** An empty or whitespace tag key fails validation. |
| `max_actions_per_run: 100000` | **Accepted, and visible.** The cap is the user's to set. It is reported in `plan`, `status` and every notification, so a raised cap is not a quiet one. |
| `include_untagged: true` | **Rejected unless** `untagged_acknowledgement` is also the exact sentence. Two keys, one of them a sentence a reviewer will notice in a diff. |
| A misspelled key, e.g. `protect_tags:` | **Rejected.** Unknown keys are errors. Crosswind warns on these; here a misspelled `protect_tag` is a resource somebody believed was protected. |
| `slack_webhook_parameter: /../../other-app/secret` | **Rejected.** Validated as an SSM parameter name: leading `/`, no `..`, no traversal. |
| A YAML bomb | `yaml.safe_load`, so no arbitrary object construction. A deeply nested document crashes rather than executes. |

**The residual risk is the limit value itself**, and it is accepted because
it cannot be designed away: the ability to say what the limit is *is* the
product. A PR that drops `amount_usd` to `1` in an account spending
thousands causes a clamp on the next reconcile.

What bounds it:

- The clamp is of **opt-in resources only**. The worst case is bounded by
  what you tagged, not by your account.
- `max_actions_per_run` bounds it again.
- Everything is **reversible**, and `clamper release --all` is one command.
- The notification says what crossed and what was stopped, so the blast is
  loud. There is no silent version of this failure.
- The config change is in a diff, and the deploy that applies it needs
  rights to update the stack.

What you should do about it: treat `clamper.yml` as production
infrastructure, because it is. Require review on it, and do not run
`clamper deploy` from a workflow triggered by a pull request. The shipped
example workflow runs `clamper plan` — which is read-only — and nothing else,
for exactly this reason.

### Someone publishes to the SNS alerts topic

The alerts topic triggers the enforcer. Anyone who can publish to it can make the
enforcer run.

**That is deliberately uninteresting**, and it is the main reason the SNS
payload is not parsed. The message is a doorbell. `reconcile()` calls
`DescribeBudget` and decides from what AWS reports *now*:

- A spoofed "you have breached $5000" message causes a reconcile that reads
  the real spend, finds no breach, and does nothing.
- A replayed real message from last month causes a reconcile against this
  month's spend.
- A message with a crafted payload — nested JSON, a huge string, an
  injection into a field — is never read at all. The handler does not look
  at `Records[*].Sns.Message` except to log its length.

The remaining risk is a denial-of-wallet: publish a million messages and pay
for a million Lambda invocations. The topic policy allows publish from
`budgets.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn`
conditions, and from the account's own principals. An attacker who can
already publish to a topic in your account can run up a bill in cheaper ways.

There is a second-order version worth naming: making the enforcer run *more
often* is not harmless, because every run can clamp. But it can only clamp
when the real spend is really over the real limit, in which case the clamp is
the correct outcome whoever asked for it.

### Someone tampers with the ledger

The ledger decides what `release` restores. A DynamoDB table someone can
write to is a way to make `release` do something odd.

- **Method names never come from the ledger.** An entry carries an
  `action_id`; the boto3 method comes from `actions.yml` via the actuator.
  Writing `"method": "terminate_instances"` into an entry does nothing,
  because nothing reads that field.
- **Resource identifiers from the ledger are validated** against the action's
  expected identifier shape before a release call is built. An entry naming
  an ARN in another account fails the account check.
- **Restored values are type-checked and bounded.** A recorded
  `DesiredCapacity` of `999999` fails validation against the group's
  `MaxSize`; a non-integer fails to parse. Release reports the entry as
  unrestorable rather than passing the value through.
- **The table's resource policy and the role scope limit who can write.** The
  enforcer role has item-level access to this one table and nothing else; the
  CLI user needs it too, which is in the deploy policy.

What this does **not** protect against: someone with write access to the
table can *delete* entries, which makes a clamp unreleasable through Clamper.
The resources are still there and still startable by hand, so this is a
nuisance rather than a loss, and point-in-time recovery is enabled on the
table so it is a recoverable nuisance.

### A resource is tagged to make it a target

Tags decide what may be clamped, so whoever can tag can enrol a resource.

This is the design working as intended — tag-based opt-in is the feature —
but it cuts both ways: `ec2:CreateTags` on a resource is enough to make it
clampable. If that matters in your account, use `selectors` with explicit
resource IDs instead of the managed tag, and restrict `ec2:CreateTags`.
[`docs/configuration.md`](configuration.md) says this where someone choosing
between the two will read it.

The inverse — removing `clamper:protect` to make a protected resource
clampable — is the same permission and the same answer. `clamper plan` shows
the protected list every time, so a resource that silently stopped being
protected is visible in the next plan, and a scheduled `clamper plan` in CI
is a cheap way to notice.

### Clamper clamps itself

An enforcer that sets its own reserved concurrency to zero cannot release
itself, and neither can anything else, because the release would be performed
by the function that is throttled. The ledger would hold the entry and
nothing could act on it. Recovery means a console visit.

So self-exclusion is three independent checks rather than one, in
`core/targets.py`:

1. The resource carries `aws:cloudformation:stack-name` equal to the stack's.
2. The resource's ARN appears in the stack's own resource list.
3. The resource is the enforcer function, the ledger table, either topic or
   the artifact bucket, matched by name.

Any one of them excludes. All three have to fail for the bad outcome, and a
test asserts each one independently excludes the enforcer with the other two
disabled.

The run-order detail that matters: self-exclusion is applied **before** the
per-run cap, so a run that is over the cap cannot have excluded the enforcer
"later".

### A clamp is reported as done when it did not happen

The mirror-image failure, and a security issue rather than a cosmetic one for
the same reason a falsely-clean Crosswind scan is.

- A failed call produces `Result(ok=False)` with the error code and message,
  and the entry settles to `failed`. It appears under **NOT CLAMPED** in
  every output format with the error attached.
- A crash between `record` and `settle` leaves the entry `pending`.
  `pending` renders as "outcome unknown", is counted separately from
  `clamped`, and makes the exit code 2.
- The counts in every report are computed from entry status, not from the
  number of targets attempted. There is no code path that increments a
  success counter before a result comes back.
- `tests/test_ledger.py` kills the process between record and call and
  asserts the entry is `pending` and is rendered as not clamped.

### Someone reads a notification, a plan, or the ledger

All three name your resources and your spend. The ledger additionally says
**what is currently stopped**, which is a list of things that will not notice
being attacked.

Mitigations are mostly yours: the notification goes to an email address you
chose, the plan is written where you asked, and `.gitignore` covers
`clamper-plan-*`, `clamper-ledger-*` and a stray `clamper.yml`. Treat them
like any internal document.

Clamper's part: no account ID, resource ID or ARN appears in any CloudWatch
log line at a level above `INFO`, log retention is 30 days rather than
forever, and the notification carries resource IDs but never tag values,
because tags are free text and people put things in them.

### Markup or control characters in a resource name

Tags and names are free text and reach every output format.

- **Terminal**: ASCII-only output and no ANSI pass-through, so no escape
  sequence injection into someone's terminal.
- **Markdown**: pipes, backticks and angle brackets escaped — an unescaped
  pipe also breaks the table.
- **JSON**: `json.dumps` handles it.
- **The notification**: plain text, and control characters are stripped.
- **The release command printed in a notification** is the one place a
  resource identifier is interpolated into something a person will paste
  into a shell. Clamp IDs are generated by Clamper and match
  `[a-z0-9-]{1,64}`; the command prints a clamp ID and never a resource
  name. A resource identifier that fails its shape check is rendered as its
  repr rather than bare.

### The enforcer's role is used by something else

The role can stop your tagged resources. Anyone who can assume it can too.

- Its trust policy allows `lambda.amazonaws.com` only, with
  `aws:SourceAccount`. No human principal, no OIDC, no cross-account.
- Its policy is scoped by tag condition where the service supports one, so
  even holding it does not grant a stop on an untagged resource — for the
  six of eight actions where that holds. See the table and the caveat in
  [`design.md`](design.md#which-iam-actions-support-a-tag-condition).
- It has no `iam:*`, no `cloudformation:*`, and no ability to change its own
  policy or the stack. An attacker holding it cannot widen it.
- It cannot terminate or delete anything, because no such action is in it.

That last point is the useful one: the worst an attacker with this role can
do is stop things that are tagged for stopping, reversibly, while writing
what they did to a ledger.

## What is not protected

The part worth reading twice.

- **Clamper cannot stop money that is already spent.** A clamp stops future
  charges from the resources it stopped. The hours already accrued are
  billed. There is no version of this tool that undoes a charge.
- **Clamper is not a cent-accurate cap, and cannot be.** Billing data lags by
  hours; see the AWS quotes in [`design.md`](design.md#budgets-refresh-cadence).
  Anyone promising you a cent-accurate hard cap on AWS is selling you
  something.
- **Some charges cannot be stopped by stopping anything.** Savings Plans and
  Reserved Instances bill whether or not you use them. Marketplace
  subscriptions and AWS Support bill on their own schedule. Data transfer
  already in flight is already incurred. These are in the README's
  Limitations because they are the things people assume a cap covers.
- **A clamp is an outage.** Clamper makes that reversible and recorded; it
  does not make it free. Stopping an RDS instance drops connections, and
  "starting a DB instance requires instance recovery and can take from
  minutes to hours"
  ([AWS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StartInstance.html)).
  A release is not instant.
- **Reversible is not lossless.** An EC2 instance with instance-store volumes
  loses that data on stop. An in-memory cache is gone. A stopped instance may
  come back with a different public IPv4 address. The design chose only
  actions whose *configuration* is restorable; it cannot restore state the
  resource was holding in RAM or on ephemeral disk.
- **The IAM tag conditions are the second layer, not the first.** The rows in
  the condition-key table were not read from AWS's authorization tables
  directly — see the caveat in `design.md`. The runtime tag check is the
  guard that matters.
- **`clamper deploy` needs broad permissions.** Creating IAM roles, a budget,
  a DynamoDB table and a Lambda is not a narrow set. It is used once, by a
  person, and `--template-only` exists so you can skip it entirely and deploy
  from the console. [`iam/README.md`](../iam/README.md) has the account of
  what the deploy policy can do.
- **Nothing stops you tagging your production database `clamper:managed`.**
  Clamper has no idea what is production. It stops what you told it it may
  stop. `clamper plan` is how you find out what you told it, and it is
  read-only, so there is no reason not to run it first.

## Residual risks

Accepted, and listed so nobody is surprised.

| Risk | Why accepted |
|---|---|
| A malicious `clamper.yml` can set an absurdly low limit | Inherent: setting the limit is the product. Bounded by opt-in targets, the per-run cap, reversibility, and a loud notification. |
| Whoever can tag can enrol or un-protect a resource | Inherent to tag-based opt-in. `selectors` is the alternative for accounts where it matters. |
| A clamp is an outage, and a release is not instant | Inherent. Documented, and `dry-run` is the default so the first run is never the live one. |
| Three of eight write actions' tag conditions are from secondary sources | Failure mode is a condition that never matches — narrower than intended, not wider. Runtime tag check is the real guard. |
| Someone with table write access can delete ledger entries | Resources remain startable by hand; PITR is on. A nuisance, not a loss. |
| The ledger says what is currently stopped | Inherent to being able to restore it. Treat it as internal. |
| Publishing to the topic can force a reconcile | The reconcile reads real spend, so it can only act when acting is correct. Denial-of-wallet bounded by Lambda's free tier and cheaper to achieve other ways. |
| Instance-store and in-memory state is lost on stop | Inherent to stopping. Named in the README's Limitations. |

## What would be a vulnerability

Please report these (see [`SECURITY.md`](../SECURITY.md)):

- **Any mutating AWS call that does not go through `core/actuator.py`**, or
  any way to make the actuator invoke a method that is not in
  `actions.yml`.
- **Any way to make Clamper clamp in `dry-run` mode.** (Release is a
  deliberate human command and ignores `mode`; it has its own `--dry-run`.)
- **Any way to clamp a resource carrying `clamper:protect`**, or one outside
  the configured scope and selection.
- **Any way to make Clamper clamp its own stack's resources.**
- **Any way to exceed `max_actions_per_run`** other than a person running
  `clamper clamp --yes`, which is the documented override and is said in
  that run's report. The enforcer never passes it.
- **A failed or unknown-outcome clamp being reported as done**, in any output
  format or notification. Same class of issue as a failed Crosswind scan
  reported as clean: the entire value of the tool is that "stopped 14
  resources" means fourteen resources stopped.
- **A clamp that cannot be released** by `clamper release` given an intact
  ledger — including one where the recorded previous state is wrong or
  incomplete.
- **A terminate or delete call reaching AWS** from any code path.
- Credentials or a Slack webhook URL written to disk, a log, a template
  parameter, or the ledger.
