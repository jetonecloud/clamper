# Design

This is the architecture, and the decisions behind it. It was written before
the code and updated as things were learned, so where a decision turned out
to be wrong the reasoning for the replacement is here too.

## The problem

AWS has no hard spending cap. AWS Budgets sends an email when you cross a
number. It does not stop the runaway GPU instance, the autoscaling group that
scaled to 200, or the Lambda in a retry loop. People find out from the
invoice.

Clamper makes a budget enforceable. When spend crosses a limit you set, it
stops the specific things you allowed it to stop, tells you exactly what it
stopped, and puts everything back when you say so.

## Why this one writes

Preflight and Crosswind are read-only, and that is their main promise. A tool
that cannot write cannot break anything, and the tests can prove it.

Clamper cannot make that promise, because a limit that cannot stop anything
is just an alert, and alerts are the thing that already does not work. So the
trust has to come from somewhere else. Four properties, in the order they
matter:

1. **Opt-in.** A resource is a candidate only because you tagged it
   `clamper:managed=true` or named it in a selector. Untagged resources are
   never touched unless you write a scope that says so, in plain words, in
   your config.
2. **Declared up front.** Every write Clamper is capable of making is a row
   in [`actions.yml`](../src/clamper/data/actions.yml). The IAM policy is
   generated from that file, so the permission Clamper asks for and the
   thing Clamper can do are the same list by construction, not by review.
3. **Recorded before it happens.** The previous state goes into the ledger
   *before* the write call. If the process dies between the two, the ledger
   says "we were about to stop this" rather than nothing.
4. **Reversible.** Every action in v1 has an inverse. `clamper release`
   restores exactly what the ledger recorded.

And the default is `dry-run`, where the enforcer decides, records and
notifies but does not act. Moving to `enforce` is an edit to your config that
you make on purpose.

## Shape of the thing

Two halves, one package.

```
your machine                           your AWS account
------------                           ----------------

clamper plan      ----- reads ----->   Budgets, EC2, ASG, ECS,
clamper status                         RDS, Lambda, SageMaker
clamper ledger    ----- reads ----->   DynamoDB ledger
clamper release   --- writes via --->  core/actuator.py
clamper clamp     --- writes via --->  core/actuator.py

clamper deploy    --- deploys ----->   +-------------------------+
                                       | CloudFormation stack    |
                                       |                         |
                                       |  Budget --> SNS --+     |
                                       |                   v     |
                                       |  EventBridge ---> Lambda|
                                       |   (6h)            |     |
                                       |                   v     |
                                       |              actuator   |
                                       |                   |     |
                                       |     DynamoDB <----+     |
                                       |     ledger        |     |
                                       |                   v     |
                                       |              SNS --> you|
                                       +-------------------------+
```

The Lambda is the same Python package as the CLI — `src/clamper/enforcer/`
imports `clamper.core` — so the rules about what may be clamped are written
once. `clamper simulate` runs the enforcer's own entry point locally against
a fake event with dry-run forced, which means the thing you test is the
thing that will run.

```
reconcile()                            enforcer/run.py
   |
   +-- read each limit's budget         core/budgets.py
   +-- classify: ok | warn | clamp      core/decide.py
   +-- if clamp:
   |      +-- discover candidates       core/resources/*.py
   |      +-- filter and cap            core/targets.py
   |      +-- for each target:
   |      |      +-- snapshot state     core/resources/*.py
   |      |      +-- ledger.record()    core/ledger.py   <-- before
   |      |      +-- actuator.invoke()  core/actuator.py
   |      |      +-- ledger.settle()    core/ledger.py   <-- after
   |      +-- re-stop anything AWS restarted
   +-- notify                           enforcer/notify.py
```

## What AWS actually does, read from the docs

Everything in this section decides what Clamper can honestly promise, so it
was read from AWS's documentation rather than recalled. Links are to the page
each sentence came from, as read in November 2026.

### Budgets refresh cadence

> "AWS Budgets information is updated up to three times a day. Updates
> typically occur 8–12 hours after the previous update."
>
> — [Managing your costs with AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)

> "AWS billing data, which Budgets uses to monitor resources, is updated at
> least once per day. Keep in mind that budget information and associated
> alerts are updated and sent according to this data refresh cadence."
>
> — [Best practices for AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html)

And the warning AWS puts on the first of those pages, which is the honest
version of Clamper's central limitation:

> "There can be a delay between when you incur a charge and when you receive
> a notification from AWS Budgets for the charge. This is due to a delay
> between when an AWS resource is used and when that resource usage is
> billed. You might incur additional costs or usage that exceed your budget
> notification threshold before AWS Budgets can notify you, and your actual
> costs or usage may continue to increase or decrease after you receive the
> notification."

**So "hard limit" here means "stopped within hours of the billing data
showing the breach", not "stopped at the exact cent".** That sentence is near
the top of the README because anyone who expects the second thing will be
angry, and they will be right to be.

### `ACTUAL` alerts fire once per period

This is the single most load-bearing fact in the design:

> "Actual alerts are only sent out once per budget, per budget period, when a
> budget first reached the actual alert threshold."
>
> "Forecast-based budget alerts are sent out on a per-budget, per-budget
> period basis. They might alert more than once in a budgeted period if the
> forecasted values exceed, dip below, and then exceed the alert threshold
> again during the budgeted period."
>
> — [Best practices for AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html)

A design that only listened to SNS would get **exactly one chance per
month**. If the Lambda was mid-deploy, throttled, or had a bug that run, the
clamp would never fire again until the first of the next month — and nothing
would say so.

That is why the EventBridge reconcile schedule is not a nicety. It is the
primary guarantee and SNS is the latency optimisation. The schedule also
covers the three other cases where one-shot delivery is not enough: a limit
added after the alert already fired, a resource created after the clamp, and
an RDS instance AWS restarted on its own (below).

### Forecasts need history

> "AWS requires approximately 5 weeks of usage data to generate budget
> forecasts. If you set a budget to alert based on a forecasted amount, this
> budget alert isn't triggered until you have enough historical usage
> information."

So `clamper status` shows "not available yet" rather than a zero when
`ForecastedSpend` is absent, and the default notifications are `ACTUAL`.
Forecast thresholds are available in config but are not the default, because
a forecast-triggered clamp on a five-week-old account would never fire and a
user would have no way to tell that from "nothing has gone wrong".

### Auto Scaling puts back what you stop

> "If a budget action is used to stop an Amazon EC2 instance in an Auto
> Scaling Group (ASG), Amazon EC2 Auto Scaling restarts the instance, or
> launches new instances to replace the stopped instance."
>
> — [Best practices for AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html)

AWS is describing its own budget actions here, and the problem is identical
for anything that stops instances. It is why Clamper's Auto Scaling action
sets the group's `MinSize` and `DesiredCapacity` to zero rather than stopping
the instances in it — and why the EC2 instance handler skips any instance
that belongs to an Auto Scaling group, leaving it to the ASG action.

Stopping an ASG's instances directly would look like it worked, and the group
would quietly replace them. "Looks like it worked" is the failure mode this
whole tool exists to avoid.

### RDS restarts itself after seven days

> "If you don't manually start your DB instance after it is stopped for seven
> consecutive days, RDS automatically starts your DB instance for you. This
> way, your instance doesn't fall behind any required maintenance updates."
>
> — [Stopping an Amazon RDS DB instance temporarily](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html)

So a clamp on RDS has a seven-day shelf life. The reconcile run notices any
ledger entry still `clamped` whose instance is `available` again, re-stops
it, and **says so in the notification** — because a database silently coming
back up a week into a cost incident is exactly the kind of thing a person
needs told rather than handled.

The same page gives the limits Clamper has to report rather than hit:

> "You can't stop a DB instance that has a read replica, or that is a read
> replica."
>
> "RDS for SQL Server doesn't support stopping a DB instance in a Multi-AZ
> deployment."

Both are reported as **not clamped**, with the reason, not skipped silently.

And the cost reality, which keeps `clamper plan`'s arithmetic honest:

> "While your DB instance is stopped, you are charged for provisioned storage
> (including Provisioned IOPS). You're also charged for backup storage,
> including manual snapshots and automated backups within your specified
> retention window. Additionally, if your DB instance is publicly accessible,
> you will continue to be charged for the public IPv4 address. However,
> you're not charged for DB instance hours."

`clamper plan` therefore quotes the **instance-hour** saving for RDS and says
in the same line that storage keeps billing. The same applies to EC2 and its
EBS volumes.

### SNS topic requirements

> "Amazon SNS topics must be in the same account as the Budgets you're
> configuring. Cross-account Amazon SNS isn't supported."
>
> "**The SNS topic is encrypted** — You have encryption enabled on the SNS
> topic. The SNS topic won't work without additional permissions. Disable
> encryption on the topic, and refresh the Budget edit page."
>
> — [Creating an Amazon SNS topic for budget notifications](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-sns-policy.html)

The topic policy AWS documents, which is what the template emits:

```json
{
  "Effect": "Allow",
  "Principal": { "Service": "budgets.amazonaws.com" },
  "Action": "SNS:Publish",
  "Resource": "<topic arn>",
  "Condition": {
    "StringEquals": { "aws:SourceAccount": "<account-id>" },
    "ArnLike": { "aws:SourceArn": "arn:aws:budgets::<account-id>:*" }
  }
}
```

Both conditions are in the template. Without `aws:SourceAccount` the topic is
publishable by the Budgets service on behalf of *any* account, which is the
confused-deputy shape.

The template leaves the topic unencrypted and says why in a comment. An
encrypted topic needs a KMS key policy granting `budgets.amazonaws.com`, and
a customer-managed key costs $1/month — more than the rest of Clamper's
running cost put together. The topic carries budget thresholds and resource
IDs, not credentials. [`docs/threat-model.md`](threat-model.md) covers what
that exposes.

> "For notifications to be sent successfully, your budget must have
> permissions to send a notification to your topic, and you must accept the
> subscription to the Amazon SNS notification topic."

The email subscription needs a human to click a link in a confirmation email.
`clamper deploy` says so when it finishes and `clamper doctor` reports a
`PendingConfirmation` subscription as a failure, because an unconfirmed
subscription is a clamp nobody will hear about.

### Budget notification shape is not a contract

A budget can carry "up to five notifications, and each notification can have
one SNS subscriber and up to 10 email subscribers"
([`AWS::Budgets::Budget`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-budgets-budget.html)),
and `NotificationsWithSubscribers` is `Update requires: Replacement` — so
changing a threshold replaces the budget rather than editing it. Worth
knowing before you wonder why your budget's history reset.

**Clamper does not parse the SNS message body.** The notification is a
doorbell: the enforcer treats any message on the topic as a reason to run
`reconcile()`, and `reconcile()` gets the numbers from `budgets:DescribeBudget`
itself. The payload format of a budget notification is not a documented,
versioned contract, and a tool that stops your production database off the
back of a regex over an unversioned string is a tool that will one day stop
your production database for the wrong reason.

It costs one API call and buys three things: SNS and EventBridge become the
same code path, a replayed or spoofed message cannot invent a breach, and the
decision always rests on the number AWS reports now rather than the number in
a message of unknown age.

### Cost allocation tag filters

A cost budget scoped to a tag uses `CostFilters` with a `TagKeyValue` entry in
the form `user:<TagKey>$<TagValue>`. Two things to know:

- The tag must be **activated as a cost allocation tag** first, and
  activation is not retroactive — AWS only applies it to usage recorded
  afterwards. `clamper doctor` checks activation through
  `ce:ListCostAllocationTags` and says so, because a tag-scoped budget on an
  inactive tag reads as `$0.00` forever and looks exactly like "nothing is
  wrong".
- Budget *resource* tags are a different thing entirely, and AWS warns:
  "AWS Budgets does not support tags for cost allocation. This means you will
  not see tag information in cost and usage data". That note is about tagging
  the budget object; it is not about `CostFilters`. It is in this document
  because it cost an hour of confusion.

### Which IAM actions support a tag condition

The generated policy scopes by tag condition wherever the service supports
one. What support exists, and how it was established:

| Action | Condition key | How confirmed |
|---|---|---|
| `ec2:StopInstances`, `ec2:StartInstances` | `aws:ResourceTag/` | Documented and widely used |
| `rds:StopDBInstance`, `rds:StartDBInstance`, `rds:StopDBCluster`, `rds:StartDBCluster` | `aws:ResourceTag/` | RDS tag-based access control |
| `ecs:UpdateService` | `aws:ResourceTag/` | ECS tag-based authorisation |
| `autoscaling:UpdateAutoScalingGroup` | `autoscaling:ResourceTag/` | Auto Scaling uses its **own** key, not `aws:ResourceTag` |
| `lambda:PutFunctionConcurrency`, `lambda:DeleteFunctionConcurrency` | `aws:ResourceTag/` | Lambda ABAC on the `function` resource |
| `sagemaker:StopNotebookInstance`, `sagemaker:StartNotebookInstance` | `aws:ResourceTag/` | SageMaker ABAC on `notebook-instance` |

The Auto Scaling row is the one that bites: a policy written with
`aws:ResourceTag` on `autoscaling:UpdateAutoScalingGroup` is not an error, it
just never matches, so the permission silently does nothing. Each action
carries its own `tag_condition` key in `actions.yml` for that reason rather
than inheriting a global default.

**What was not verified first-hand:** the AWS Service Authorization Reference
pages render their tables with JavaScript and could not be read directly, so
the six rows above come from service-level ABAC documentation and secondary
sources rather than from the authorization table itself. That is weaker
evidence than the quotes earlier in this document, and it is called out here
rather than presented as equivalent. The consequence of a wrong row is a
condition that never matches and a permission that is therefore narrower than
intended — a clamp reported as failed, not a clamp that happens when it
should not. The runtime tag check in `core/targets.py` is the guard that
matters; the IAM condition is the second layer.

## What Clamper costs to run

A cost-control tool that costs money is a bad joke, so here is the bill, for
the default configuration: one limit, one reconcile every six hours, a
handful of clamps a year.

| Thing | Rate | Monthly |
|---|---|---|
| AWS Budgets, one cost budget | Free. "You can monitor and receive notifications on your budgets free of charge." ([pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/)) | **$0.00** |
| `budgets:DescribeBudget` | Not charged | **$0.00** |
| Lambda, ~124 invocations (4/day) at 256 MB for ~3s | 1M requests and 400,000 GB-s free per month, forever | **$0.00** |
| DynamoDB ledger, on-demand | A few writes per clamp; 25 GB storage free tier | **$0.00**, under a cent without it |
| EventBridge scheduled rule | Scheduled rules are not charged | **$0.00** |
| SNS, a few emails | First 1,000 email notifications per month free | **$0.00** |
| CloudWatch Logs | ~1 KB per run, 5 GB/month free | **$0.00** |
| S3 artifact bucket, one ~60 KB zip | Rounds to nothing | **$0.00** |

So: **nothing, in the common case**, and cents a month for an account past
every free tier. Three deliberate choices keep it there.

**No budget actions.** AWS Budgets can take actions itself, and that would be
less code. But: "Your first two action-enabled budgets are free (regardless
of the number of actions you configure per budget) per month. Afterwards each
subsequent action-enabled budget will incur a $0.10 daily cost" — $3/month
per budget past the second. Clamper uses a plain notification to SNS, which is
free at any number of budgets, and does the acting itself. It also needs to,
because budget actions cannot scale an ASG to zero or set a Lambda's reserved
concurrency, and cannot write a ledger.

**No Cost Explorer.** `ce:GetCostAndUsage` is "$0.01 per request"
([pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)).
Four reconciles a day across three limits is 360 requests a month — $3.60/month
to find out what Budgets will tell you for free. Clamper reads
`budgets:DescribeBudget`, which returns `CalculatedSpend.ActualSpend` and
`ForecastedSpend` and is not charged. Cost Explorer is used in exactly one
place: `clamper doctor`'s single `ce:ListCostAllocationTags` call, which is
a different (free) API, and `clamper plan`'s per-hour figures come from a
static price table rather than from AWS.

**No always-on anything.** No scheduled Lambda warmer, no provisioned
concurrency, no EventBridge rule firing every minute to feel responsive.
Budget data moves three times a day; polling it every six hours is already
twice as often as it changes.

The one thing with a real price tag is in the bootstrap stack: the S3 artifact
bucket. It is created once, holds one zip, and
`clamper deploy --template-only` shows you both templates before any of it
exists.

## Config

`clamper.yml`, read from the repository you deploy from —
[`docs/configuration.md`](configuration.md) is the reference. The design
decisions worth recording:

**Unknown keys are errors, not warnings.** Crosswind warns, because a
misspelled key there means a severity rule that did not apply and a report
that is slightly wrong. Here a misspelled key could mean
`protect_tag: clamper:protekt` and a resource somebody believed was protected
being stopped. The failure modes are not comparable, so the handling is not
either.

**`mode: dry-run` is the default, and it is in the file rather than implied.**
`clamper init` writes it explicitly with a comment. A default that is absent
from the file is a default nobody reads.

**Including untagged resources takes a sentence, not a flag.** The key is
`include_untagged`, it defaults to `false`, and setting it to `true` requires
`untagged_acknowledgement` to be the exact string
`"I understand this can stop resources nobody tagged"`. That is deliberately
annoying. The difference between "stop the things I labelled" and "stop
everything you can find" is the difference between a tool and an incident,
and it should not be one character of YAML.

**`clamper:protect` wins over everything.** It is checked last, after
selectors, after `include_untagged`, after an explicit resource ID. There is
no config key that overrides it and no `--force` that ignores it. A tag you
can set in the console and be *certain* about is worth more than the
flexibility of being able to override it.

**Thresholds are percentages of the limit, not absolute amounts.** `warn`
defaults to 80 and `clamp` to 100. Percentages because that is what Budgets
notifications take, so the number in your config and the number in the
notification AWS sends are the same number.

## Actions

[`src/clamper/data/actions.yml`](../src/clamper/data/actions.yml) declares
every write Clamper can make. One row per action, carrying the resource type,
the boto3 method, the IAM action it needs, the tag condition key that scopes
it, which fields of the previous state are recorded, and the inverse.

Four consequences of putting this in a data file:

- **The IAM policy is generated from it.** `core/policy.py` reads the same
  rows. There is no second list to keep in step, which is the usual way a
  least-privilege policy rots into a wildcard.
- **The actuator's allowlist is derived from it.** A method not in
  `actions.yml` cannot be called, because the actuator looks the method name
  up by action id rather than taking one from its caller.
- **Destructive verbs are refused at load.** An action whose boto3 method or
  IAM action begins with a `Terminate` or `Delete` verb raises
  `DestructiveAction` when the registry loads, which means the package fails
  to import rather than shipping with the row in it. The one exception is
  `lambda:DeleteFunctionConcurrency`, which is named in an allowlist with the
  reason written next to it: deleting a *concurrency setting* restores a
  function to its default, and it is the only correct release for a function
  that had no reserved concurrency before. The exception is a single named
  string, not a pattern.
- **You can read it.** The whole set of things this tool can do to your
  account is one short file.

### v1 actions

| Target | Clamp | Release |
|---|---|---|
| EC2 instances | `StopInstances` | `StartInstances`, only those Clamper stopped |
| Auto Scaling groups | `MinSize`/`DesiredCapacity` → 0 | restore recorded min/max/desired |
| ECS services | `desiredCount` → 0 | restore recorded count |
| RDS instances | `StopDBInstance` | `StartDBInstance` |
| Aurora clusters | `StopDBCluster` | `StartDBCluster` |
| Lambda functions | reserved concurrency → 0 | restore recorded value, or delete the setting if there was none |
| SageMaker notebook instances | `StopNotebookInstance` | `StartNotebookInstance` |

Every one of these is reversible and none loses data. That is the whole
selection criterion for v1.

### What is deliberately not here

**Nothing that terminates or deletes.** No `TerminateInstances`, no
`DeleteStack`, no scaling an EKS node group's instances away. The registry
refuses to load such a row, so this is a property of the code rather than a
policy in a document.

**SageMaker endpoints.** An endpoint is the expensive SageMaker thing — a
`ml.p4d.24xlarge` endpoint is the shape of runaway cost this tool is for. It
is not in v1 because the only way to stop an endpoint's cost is
`DeleteEndpoint`, and the inverse is `CreateEndpoint` with a config that may
reference a model that has since been deleted. That is not a restore, it is a
rebuild that might fail — and a "release" that silently fails to bring back a
production inference endpoint is worse than never having stopped it.
`UpdateEndpointWeightsAndCapacities` cannot go to zero instances.
`clamper plan` lists endpoints it found under "not clamped", with this reason,
so the biggest number on the page is visible even though Clamper will not
touch it.

**EKS node groups.** Scaling a managed node group to zero *is* reversible
(`UpdateNodegroupConfig` with `scalingConfig.desiredSize: 0`, restoring the
recorded sizes). It is out of v1 for a different reason: in-flight pods are
evicted with no regard for `PodDisruptionBudget`s, StatefulSet volumes get
orphaned, and the cluster's own control plane keeps billing anyway, so the
saving is partial and the damage is not. It is the most likely v2 addition and
it needs a design that drains rather than scales.

**Anything data-plane.** Clamper does not cap data transfer, does not touch
S3, and cannot stop already-accrued charges. Those are in the README's
Limitations section because they are the things people assume a "hard limit"
covers.

## The one write path

`core/actuator.py` is the only module in the package that invokes a mutating
AWS API. Everything else — the resource handlers, the enforcer, the CLI —
builds a `Call(action_id, phase, params)` and hands it over. The actuator
then:

1. Looks the action up in the registry. An unknown id is `UnknownAction`.
2. Takes the **method name from the registry**, not from the caller. A caller
   cannot ask for `terminate_instances` because a caller does not get to name
   a method at all.
3. Checks the IAM action against the allowlist built from `actions.yml`.
4. Checks the params against the action's declared parameter names. An extra
   key is an error, because `Force=True` arriving as an unchecked passthrough
   is the shape of this class of bug.
5. If `dry_run`, returns a `Result` marked `dry_run=True` **without touching
   boto3**. There is no code path through `invoke` that reaches a client when
   `dry_run` is set, which is one `if` at the top rather than a flag each
   handler is trusted to honour.
6. Otherwise makes the call, and returns a `Result` carrying either success
   or the `ClientError`'s code and message.

A failed call is a `Result` with `ok=False`, never an exception that unwinds
the run. One target Clamper could not stop must not stop it trying the other
nineteen, and must not be counted as done.

`tests/test_safety.py` asserts this structurally, because the risk is a
*second* write path appearing rather than this one changing:

- **No boto3 method name appears in the Python source at all.** The plan was
  "the names appear in `actuator.py` and nowhere else"; resolving the method
  with `getattr(client, name)` where `name` comes from the registry removed
  the need for them to appear even there, which is a stronger property for
  less code. A literal `"stop_instances"` anywhere in the package is now
  either a second dispatch or a comment that should be reworded.
- **Dynamic `getattr` dispatch happens only in `core/actuator.py`.** A
  `getattr` with a literal attribute name is a field lookup and is fine
  anywhere; one with a computed name is the thing that can call anything.
- **Every call on a boto3 client outside the actuator is a `Describe`/`Get`/
  `List`.** This is the brief's check, and it is possible only because the
  package keeps a convention `CONTRIBUTING.md` states: a boto3 client is
  bound to a local called `client` or something ending `_client`. That makes
  "a call on a client" statically visible.
- **No known destructive AWS operation appears anywhere in the package** —
  `terminate_instances`, `delete_db_instance`, `delete_stack` and thirty
  others, named one at a time rather than matched as a verb pattern. A
  pattern over `release_`/`remove_` would flag `release_call` and
  `remove_prefix` and get itself suppressed, which is how that kind of check
  dies.
- **`subprocess`, `eval`, `exec` and unsafe `yaml.load` appear nowhere.**
  Crosswind needs exactly one subprocess call site, for the Terraform
  binary. Clamper needs none, so the whole category is banned rather than
  constrained.
- **boto3 clients are constructed in `core/session.py` only**, and boto3 is
  imported only there. A module that can build its own client can call
  anything on it, which makes the allowlist decoration.
- **The actuator's allowlist equals the registry's, and the set is pinned**,
  so widening it has to be a deliberate edit to a test.
- **No module under `core/` or `enforcer/` imports `rich`, `typer` or
  `click`.** None of them are in the deployment zip, so a module here that
  imports one fails at cold start — a bad place to find out.

One test in that file exists because of a gap in Crosswind's equivalent:
`test_there_are_python_files_to_check`. Every other test is driven by a
`rglob`, and a refactor that moved the package would turn the whole suite
green and empty.

## The ledger

A DynamoDB table, created by the stack. It is what makes the whole thing
reversible, so it is write-ahead and it is never silently pruned.

```
pk = clamp_id        sk = "#clamp"            the run: when, why, which limit,
                                              mode, actor, spend at the time
pk = clamp_id        sk = "<target key>"      one per target: resource id,
                                              action id, previous state,
                                              applied state, status, error
```

A GSI on `status` and `created_at` answers "what is clamped right now"
without a scan, which is what `clamper status` and `clamper release --all`
need.

### Write-ahead, specifically

```
entry = ledger.record(clamp_id, target, previous_state)   # status=pending
result = actuator.invoke(call)
ledger.settle(entry, result)                              # clamped | failed
```

The `record` happens before the `invoke`, and it carries the full previous
state. The orderings that were considered and rejected:

- **Call first, then record.** The window is small and the consequence is
  total: a process killed between the two leaves a stopped resource with no
  record of what it was, and `clamper release` cannot restore what it does
  not know.
- **Record and call in a transaction.** There is no such thing across
  DynamoDB and EC2.
- **Record `clamped` up front and correct it on failure.** Then a crash mid-run
  leaves entries claiming success for calls that never happened, and
  `release` tries to start instances that were never stopped. `pending` is
  the only honest state for "we have not found out yet".

So a crash leaves `pending` entries. `pending` is reported as **not clamped,
outcome unknown** — never as done, and never as clean. The next reconcile
re-checks the resource's live state and settles the entry from what it finds.

### Conflict detection on release

`record` stores two states: `previous_state`, which is what to restore, and
`applied_state`, which is what Clamper set it to. At release time the live
resource is read and compared to `applied_state`:

- Matches → restore `previous_state`.
- Differs → **conflict**. Somebody changed this by hand since the clamp.
  Reported, left alone, and the entry stays `clamped`. `--force` restores
  anyway, and says in the output that it overrode a conflict.

Without `applied_state` the check is not possible: "the instance is running"
could mean "somebody started it" or "the clamp failed", and those need
opposite handling.

### Never pruned

No TTL attribute, and no `clamper prune`. The ledger is the record of every
time this tool changed something in your account, which makes it an audit
log, and an audit log that deletes itself after 30 days is not one. It is
tiny — a few hundred bytes per target — and DynamoDB's free tier is 25 GB.

A user who wants it gone can delete the table; the stack will recreate it
empty, and `clamper release` will then have nothing to restore, which is why
the table carries `DeletionPolicy: Retain`.

## Targets

`core/targets.py` turns "what exists" into "what may be clamped", in a fixed
order, and attaches a reason to everything it drops. Every skip is reported —
a resource that was not clamped is never silently absent.

1. **In scope.** The limit's scope: the whole account, or resources carrying
   a cost allocation tag's key and value.
2. **Enabled type.** The action is in `types` and loaded.
3. **Selected.** Either tagged `clamper:managed=true`, or named in a
   selector, or `include_untagged` is true with its acknowledgement.
4. **Not protected.** `clamper:protect` on the resource, any value. Checked
   after everything else and overridable by nothing.
5. **Not Clamper's own.** Excluded by three independent checks, because this
   one has to hold: the resource is tagged `aws:cloudformation:stack-name`
   with the stack's name; its ARN appears in the stack's own resource list;
   or it is the enforcer function, the ledger table, the topic or the
   artifact bucket by name. An enforcer that sets its own reserved
   concurrency to zero cannot release itself, and nothing else can either.
6. **Clampable now.** An instance already `stopped` is not a target. An RDS
   instance with a read replica is reported as not clamped with the reason,
   because AWS refuses it.
7. **Under the cap.** `max_actions_per_run`, default 50.

### Why the cap stops rather than truncates

Over the cap, Clamper clamps **nothing** and notifies. It does not clamp the
first 50.

A plan with 200 targets in an account that normally has 12 is far more likely
to be a config mistake, a tag applied by a script to everything, or a bug in
this tool, than it is to be a real need to stop 200 things. Truncating would
act on an arbitrary 50 of them — chosen by whatever order the API returned —
leaving the account half-stopped in a way nobody designed, and the ledger
holding a release that restores half a system.

Stopping with a notification that says "this is above your cap of 50, here
is the list, raise the cap or run `clamper clamp --yes` if you meant it"
keeps the decision with a person for the one case where it matters most. The
cost of being wrong in this direction is money; in the other direction it is
an outage nobody asked for.

## Least privilege, generated

`core/policy.py` emits the enforcer's IAM policy from the enabled rows of
`actions.yml`:

- Read actions for discovery (`Describe*`/`Get*`/`List*`), which cannot be
  resource-scoped for most services because discovery is enumeration.
- Write actions, each scoped to its resource ARN pattern and conditioned on
  the managed tag with that action's own condition key.
- The ledger table, the topic, and `budgets:DescribeBudget` on the specific
  budget ARNs.

And it refuses to emit:

- `*` as an action, or `service:*`.
- Any action whose verb is a terminate or delete verb, bar the single named
  `lambda:DeleteFunctionConcurrency` exception.
- A write statement with `Resource: "*"` and no condition.

Same rule as Preflight, inverted: Preflight refuses to emit a policy
containing a write verb, Clamper refuses to emit one containing a
*destructive* verb or an unscoped write. `tests/test_policy.py` holds both the
generated policy and the CloudFormation template to it.

The deploy policy — what the human running `clamper deploy` needs — is
separate, broader, and lives in [`iam/deploy-policy.json`](../iam/deploy-policy.json)
with its own account of what it can do. It is used once and is not what the
enforcer runs as.

## The stack

Two stacks, because a Lambda's code has to be somewhere before the stack that
references it exists.

**Bootstrap** (`clamper-bootstrap`): an S3 bucket for the deployment zip.
Versioned, encrypted with SSE-S3, public access blocked, `BlockPublicPolicy`
on, and a lifecycle rule expiring old versions after 30 days.

**Main** (`clamper`): the budget, the topic and its policy, the ledger table,
the enforcer function and its role, the EventBridge rule, and the log group
with a 30-day retention so logs do not accumulate forever.

`clamper deploy --template-only` writes both to a directory and makes **no
AWS calls at all** — no `sts:GetCallerIdentity`, nothing. You can read the
entire thing, run `cfn-lint` over it, and deploy it by hand from the console
if you would rather not give a CLI `cloudformation:CreateStack`. That is a
first-class path, not a debugging aid: Preflight does the same for its role
template, and it is how a careful person evaluates a tool that is going to be
able to stop their infrastructure.

The templates are generated by `core/stack/*.py` rather than being static
files, because the IAM policy inside the main stack comes from `actions.yml`
and a static template would be a copy to keep in step. The generator emits
plain YAML with no CloudFormation macros and no transforms, so what you read
is what deploys.

## Reports

Three renderers over one object, each a pure function of it.

- **Terminal.** Plain ASCII, no ANSI, no box-drawing. It has to survive a CI
  log, a ticket, and an email from someone pasting it. Ordered by cost, so
  the expensive thing is the first thing read.
- **Markdown.** For a PR comment or a GitHub job summary.
- **JSON.** Versioned, documented in [`json-schema.md`](json-schema.md).
  Additive changes bump the minor, breaking ones the major.

No HTML. Crosswind has one because a drift report goes to a manager who did
not run the tool; Clamper's output goes to whoever is on call, and the format
that matters is the one that survives being pasted into Slack.

## Notifications

Plain language, to the SNS topic, saying five things in this order: what
crossed, which limit, what was clamped, what was **not** and why, and the
exact `clamper release` command to undo it.

The release command is in the message because the person reading it at 3am
did not write the config, may not have Clamper installed, and needs the
shortest possible path from "something stopped" to "I have put it back".

A Slack webhook is optional. It goes in SSM Parameter Store as a
`SecureString` and the parameter *name* goes in the config; the webhook URL
itself never appears in the template, the config, or the ledger. A Slack
webhook URL is a credential — anyone holding it can post to that channel as
you — and CloudFormation template parameters are visible to anyone with
`cloudformation:DescribeStacks`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing to do: under every limit, or nothing to clamp |
| 1 | A limit is breached, or a clamp is active |
| 2 | Something could not be checked or acted on, or the tool failed |

2 beats 1, as in Crosswind. A target Clamper could not stop is a hole in the
enforcement rather than a finding in a report, and it is the more urgent of
the two.

## What is deliberately not here

A dashboard, a hosted service, a daemon, multi-account orchestration, other
clouds, and any action that loses data. Also: no telemetry, no upload,
nothing to opt out of.

The most-requested thing will be "stop everything, not just what I tagged".
That is `include_untagged: true` plus an acknowledgement string, and it is
deliberately hard to type. The second will be "clamp faster". That one is not
a Clamper feature, it is an AWS billing-data latency problem, and anyone
promising you a cent-accurate hard cap on AWS is selling you something.
