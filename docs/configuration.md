# Configuration

`clamper.yml` decides what Clamper may stop and when. That makes it the most
consequential input to the tool, so it is validated strictly and this page
describes every key.

`clamper init` writes a starting file with every default spelled out and a
comment on each. [`examples/clamper.yml`](../examples/clamper.yml) is a
fuller example. `clamper validate` checks a file without touching AWS.

## Where it is read from

The CLI uses `--config PATH`, or the nearest `clamper.yml` (or
`clamper.yaml`) in the current directory or any parent. There is no implied
configuration: with no file, every command that needs one exits 2 and tells
you to run `clamper init`.

The enforcer Lambda reads the copy `clamper deploy` packed into its zip. **A
change to `clamper.yml` does nothing until you run `clamper deploy` again**,
which builds a new zip and updates the stack. That is deliberate: changing
what can be stopped needs the rights to update the stack, not just the
rights to edit a file.

## Strictness

- **An unknown key is an error, not a warning.** `protect_tags:` instead of
  `protect_tag:` would otherwise mean a resource somebody believed was
  protected being stopped. The error names the closest valid key.
- **`version` is required** and must be `1`.
- **No key names a boto3 method, an IAM action or an ARN.** Resource types
  are referenced by the action ids in
  [`actions.yml`](../src/clamper/data/actions.yml), and an unknown id is an
  error. A config file cannot name something for Clamper to call.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `version` | required | Schema version. Must be `1`. |
| `mode` | `dry-run` | `dry-run`: the enforcer decides, records and notifies, and stops nothing. `enforce`: it stops what the plan says. |
| `stack_name` | `clamper` | The CloudFormation stack. Budgets (`<stack>-<limit id>`), the ledger table (`<stack>-ledger`), the function (`<stack>-enforcer`) and both topics are named from it. Letters, digits and hyphens, starting with a letter. |
| `regions` | your default region | Regions to look for resources in. Budgets are account-wide; resources are not. |
| `limits` | required | One or more limits. See below. |
| `targets` | see below | What may be stopped. |
| `safety` | see below | Limits on Clamper itself. |
| `notifications` | none | Where a clamp is announced. |

Start in `dry-run`. Move to `enforce` after you have read a `clamper plan`
and received at least one dry-run notification, so you know both that the
list is right and that the email arrives. `clamper release` ignores `mode`:
a clamp made in `enforce` can always be released, whatever the file says
now.

## `limits`

```yaml
limits:
  - id: whole-account
    amount_usd: 2000
    warn_percent: 80
    clamp_percent: 100

  - id: team-ml
    amount_usd: 600
    scope:
      kind: tag
      tag: team
      value: ml
```

| Key | Default | Meaning |
|---|---|---|
| `id` | required | Lowercase letters, digits and hyphens. Unique. Becomes part of the budget's name. |
| `amount_usd` | required | The monthly limit in US dollars. |
| `warn_percent` | `80` | A warning notification at this percentage of the amount. |
| `clamp_percent` | `100` | Clamp at this percentage. Above 100 is allowed — "clamp at 120%" is a real policy — up to 1000. Must not be below `warn_percent`. |
| `notification_type` | `actual` | `actual` or `forecasted`. See below. |
| `scope` | whole account | `kind: account`, or `kind: tag` with `tag` and `value`. |

Thresholds are percentages because Budgets notifications take percentages, so
the number in your config and the number in the alert AWS sends are the same
number.

**Each limit becomes an AWS budget**, and AWS replaces a budget (resetting
its history) when its notifications change. Changing a threshold is
therefore a replacement, not an edit.

**`forecasted` is not the default for a reason.** AWS needs about five weeks
of history before it produces a forecast, and a forecast-triggered limit on
a new account never fires — with nothing to tell you so. The scheduled
reconcile reads actual spend either way.

**A tag scope needs an activated cost allocation tag.** The budget filters
on `user:<tag>$<value>`, which only works once the tag is activated in the
Billing console under *Cost allocation tags*, and AWS applies activation only
to usage recorded afterwards. Before that, a tag-scoped budget reads $0.00
forever, which looks exactly like nothing being wrong. `clamper doctor` fails
until the tag is active.

A tag scope also narrows what that limit clamps: when only `team-ml` is
breached, only resources carrying `team=ml` are candidates.

## `targets`

```yaml
targets:
  managed_tag: clamper:managed
  managed_tag_value: "true"
  protect_tag: clamper:protect
  types: [ec2-instance, autoscaling-group, ecs-service, rds-instance,
          aurora-cluster, lambda-function, sagemaker-notebook,
          redshift-cluster]
  selectors:
    - type: ec2-instance
      ids: [i-0123456789abcdef0]
```

| Key | Default | Meaning |
|---|---|---|
| `managed_tag` | `clamper:managed` | A resource carrying this tag with `managed_tag_value` is a candidate. |
| `managed_tag_value` | `"true"` | Quote it: YAML reads a bare `true` as a boolean. |
| `protect_tag` | `clamper:protect` | A resource carrying this tag, **with any value**, is never stopped. Nothing in this file overrides it, and neither does `--force` or `--yes`. |
| `types` | all eight | Which action ids may be used. Remove a line to turn a type off. An empty list is an error. |
| `selectors` | none | Opt in by explicit id, per type, without a tag. |
| `include_untagged` | `false` | See below. |
| `untagged_acknowledgement` | none | See below. |

The action ids, with what each does, are in [`actions.md`](actions.md). The
order in which a resource is checked — scope, type, selection, protect,
self-exclusion, clampable now, the cap — is in
[`design.md`](design.md#targets).

### Tags or selectors?

A tag is opt-in that **anyone who can tag the resource can grant**. If
`ec2:CreateTags` is widely held in your account, that is a lot of people
who can make an instance clampable — or remove `clamper:protect` from one.
A selector is opt-in that only somebody who can change `clamper.yml` and
redeploy can grant. In an account where that difference matters, use
selectors and restrict who can tag.

`clamper plan` lists every protected resource on every run, so one that
stopped being protected shows up in the next plan. A scheduled plan in CI
([`examples/workflows/clamper-plan.yml`](../examples/workflows/clamper-plan.yml))
is a cheap way to notice.

### `include_untagged`

Setting it to `true` makes every resource of an enabled type a candidate,
tagged or not, except those carrying the protect tag. It requires a second
key alongside it, with this exact text:

```yaml
targets:
  include_untagged: true
  untagged_acknowledgement: "I understand this can stop resources nobody tagged"
```

That is deliberately annoying. The difference between "stop what I labelled"
and "stop everything you can find" is the difference between a tool and an
incident, and it should be visible to a reviewer in a diff.

## `safety`

| Key | Default | Meaning |
|---|---|---|
| `max_actions_per_run` | `50` | Above this many targets in one run, Clamper clamps **nothing** and notifies. Not the first fifty: nothing. |
| `reconcile_hours` | `6` | How often the scheduled reconcile runs. 1 to 24. |

The cap stops rather than truncates because a run far bigger than an account
normally has is more likely a stray tag or a mistake than a real need, and
stopping an arbitrary fifty of them would leave a system half-off in a way
nobody designed. If the list is right, a person runs `clamper clamp --yes`;
the scheduled enforcer is always bound by the cap.

Budgets data refreshes up to three times a day, so a six-hour reconcile is
already twice as often as the numbers move. The schedule is the primary
guarantee rather than a fallback: AWS sends an actual-spend alert once per
budget per period, so without it one missed delivery would mean no clamp
until next month.

## `notifications`

| Key | Meaning |
|---|---|
| `email` | One address, subscribed to the notifications topic by `clamper deploy`. More can be subscribed to the topic by hand afterwards. |
| `slack_webhook_parameter` | The **name** of an SSM `SecureString` parameter holding a Slack incoming-webhook URL, e.g. `/clamper/slack-webhook`. |

AWS sends a confirmation email to the address. **Nothing is delivered until
somebody clicks the link in it**, and `clamper doctor` reports an unconfirmed
subscription as a failure.

The Slack webhook URL itself never goes in this file, the template or the
ledger: a webhook URL is a credential, and CloudFormation parameters are
readable by anyone with `cloudformation:DescribeStacks`. Create the
parameter yourself:

```sh
aws ssm put-parameter --name /clamper/slack-webhook --type SecureString \
  --value 'https://hooks.slack.com/services/...'
```

The enforcer's role is granted `ssm:GetParameter` on that one parameter.

A notification says five things, in this order: what crossed, which limit,
what was stopped, what was **not** stopped and why, and the exact
`clamper release` command to undo it. When one is sent is described in
[`design.md`](design.md#notifications).

## Validation errors you might see

| Error | What to do |
|---|---|
| `unknown key at targets: protect_tags` ... `did you mean protect_tag?` | Fix the spelling. Unknown keys are never ignored. |
| `` `targets.types` names ['ec2'] `` | Use the action ids: `ec2-instance`, not `ec2`. See [`actions.md`](actions.md). |
| `` `include_untagged: true` also needs untagged_acknowledgement `` | Both keys, with the exact sentence. |
| `` `warn_percent` (90) is above `clamp_percent` (80) `` | The warning has to come first. |
| `` `safety.reconcile_hours` is 48 `` | 24 at most. A breach should not sit unenforced for days. |
| `` `notifications.email` ... is not a single email address `` | One address. Subscribe more to the topic after deploying. |
