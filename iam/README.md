# IAM

Three policies, for three different principals. Two of them are generated
from [`actions.yml`](../src/clamper/data/actions.yml) and one is written by
hand. None of them is the same as the others, and the difference is the
point.

| File | Who uses it | How often | Generated |
|---|---|---|---|
| [`enforcer-policy.example.json`](enforcer-policy.example.json) | The enforcer Lambda's role | Every reconcile | Yes |
| [`operator-policy.example.json`](operator-policy.example.json) | A person running `plan`, `status`, `release`, `clamp` | Whenever | Yes |
| [`deploy-policy.json`](deploy-policy.json) | A person running `clamper deploy` | Once, and on upgrades | No |

Unlike Preflight and Crosswind, none of these is read-only. Clamper has to
write, because a limit that cannot stop anything is just an alert. What the
policies promise instead is narrower and checkable.

## The enforcer's policy

This is the one that matters, because it runs unattended. The copy here is
what the generator emits for the default config in account `123456789012`;
yours is generated from your config by `clamper deploy` and is inside the
template it writes. `clamper policy --kind enforcer --account <id>` prints it.

What it grants:

- **Discovery reads** for the enabled resource types, on `Resource: "*"`.
  Discovery is enumeration and cannot be resource-scoped. Every one is a
  `Describe`, `Get` or `List` action; the registry refuses to load a row
  whose discovery list contains anything else.
- **Each clamp and release write**, scoped to that resource type's ARN
  pattern in your account, and conditioned on your managed tag using **that
  action's own condition key**. Auto Scaling uses `autoscaling:ResourceTag/`;
  a policy written with `aws:ResourceTag/` on
  `autoscaling:UpdateAutoScalingGroup` is not an error, it simply never
  matches. See [`docs/design.md`](../docs/design.md#which-iam-actions-support-a-tag-condition)
  for how each key was established, and which rows came from secondary
  sources.
- Explicit resource ARNs for anything you opted in with `selectors`, since a
  selector is opt-in without a tag. If you set `include_untagged`, the write
  statement is repeated without the tag condition — still scoped to your
  account's ARN pattern, never `*`.
- `budgets:ViewBudget` on your Clamper budgets by name. (`ViewBudget` is the
  permission behind the `DescribeBudget` API.)
- `GetItem`, `Query` and `PutItem` on the ledger table. **No `DeleteItem`.**
  The ledger is never pruned, and that is a property of the permission as
  well as the code.
- `sns:Publish` to its own topic, its own log group, and `ssm:GetParameter`
  on the one Slack webhook parameter you named, if you named one.

What it cannot do, and the generator refuses to emit:

- Any `*` or `service:*` action.
- Any terminate, delete or other destructive verb, except the single named
  `lambda:DeleteFunctionConcurrency` — which removes a *concurrency
  setting*, not a function, and is the only correct release for a function
  that had none.
- A write on `Resource: "*"` with no condition.
- Anything in `iam:*`, `cloudformation:*` or `sts:*`. A person holding this
  role cannot widen it or change the stack it belongs to.

The worst somebody holding this role can do is stop things that are tagged
for stopping, reversibly, while writing what they did to a ledger.

`tests/test_policy.py` checks every one of these rules, and checks that the
example files here are exactly what the generator emits today.

## The operator's policy

For a person or a CI job running `clamper plan`, `status`, `ledger`,
`release` and `clamp`. The enforcer's writes and ledger access, plus the
reads those commands make that the enforcer never does: SageMaker endpoint
and Redshift Serverless workgroup visibility for the plan, the stack's own resource list for self-exclusion,
and, for `doctor`, the topic's subscriptions, `ce:ListCostAllocationTags`,
the reconcile rule (`events:DescribeRule`) and the enforcer's configuration
(`lambda:GetFunctionConfiguration`), each scoped to Clamper's own resource.

`clamper plan` alone needs only the reads. If you run it from CI — and a
scheduled `clamper plan` is a cheap way to notice a resource that silently
stopped being protected — give that job a role with the `Discover`,
`ReadOwnBudgets` and `OwnStack` statements and nothing else.

## The deploy policy

What `clamper deploy` needs to create both stacks: an S3 bucket, an IAM role
and its inline policy, a Lambda function, a DynamoDB table, an SNS topic, a
budget, an EventBridge rule and a log group. That is not a narrow set, and
this file does not pretend it is.

It is held to a lower bar than the other two, deliberately, and here is
exactly how:

- **It contains delete actions.** CloudFormation rolls back a failed stack
  creation using the caller's permissions, and replaces a resource on some
  updates. Without `iam:DeleteRolePolicy`, `lambda:DeleteFunction` and their
  neighbours, a failed first deploy leaves a half-created stack nobody can
  roll back. Every delete is scoped to a `clamper*`-named resource.
- **It cannot delete the ledger.** There is no `dynamodb:DeleteTable`, and the
  table carries `DeletionPolicy: Retain`. Deleting the record of what is
  currently stopped should take more than a mistyped stack command.
- **It can create an IAM role and pass it.** That is what makes it powerful:
  whoever holds it can create a role named `clamper*` with any policy. It is
  scoped by name, not by content. Treat it like any deploy credential.
- Every resource is scoped by the `clamper` prefix. **If you set a custom
  `stack_name`, replace `clamper` with it throughout.**

It is used once, by a person, and is not what the enforcer runs as. If you
would rather not hold it at all, `clamper deploy --template-only` writes both
templates to a directory and makes no AWS calls, and you can deploy them from
the console with whatever role your organisation already uses for
CloudFormation.
