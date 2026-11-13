# Actions

Everything Clamper can do to your resources, one action at a time. The
source of truth is [`actions.yml`](../src/clamper/data/actions.yml): this page
explains it, and if the two ever disagree the YAML is right and this page is
a bug.

Each action is a pair of calls, a clamp and its inverse. Twelve boto3 calls
in all, pinned by a test, and every one goes through the single write path in
`core/actuator.py`. The enforcer's IAM policy is generated from the same rows,
so the permissions Clamper asks for and the things it can do are one list.

| Id (for `targets.types`) | Clamp | Release | Records |
|---|---|---|---|
| `ec2-instance` | `ec2:StopInstances` | `ec2:StartInstances` | `state` |
| `autoscaling-group` | `autoscaling:UpdateAutoScalingGroup`, min and desired to 0 | the same call, with the recorded sizes | `min_size`, `max_size`, `desired_capacity` |
| `ecs-service` | `ecs:UpdateService`, desired count to 0 | the same call, with the recorded count | `desired_count` |
| `rds-instance` | `rds:StopDBInstance` | `rds:StartDBInstance` | `status` |
| `aurora-cluster` | `rds:StopDBCluster` | `rds:StartDBCluster` | `status` |
| `lambda-function` | `lambda:PutFunctionConcurrency`, reserved concurrency 0 | the recorded value, or `lambda:DeleteFunctionConcurrency` if there was none | `reserved_concurrent_executions` |
| `sagemaker-notebook` | `sagemaker:StopNotebookInstance` | `sagemaker:StartNotebookInstance` | `status` |

"Records" is what goes into the ledger *before* the clamp call. It is what
`clamper release` restores, and what it compares the live resource against
to detect a hand-made change since.

## What each one does to you

A clamp is an outage, chosen because it is reversible. These are the
consequences worth knowing before you tag something. `clamper plan` shows
the relevant ones under each target.

### EC2 instances

- EBS volumes keep billing while the instance is stopped. The saving is the
  instance hours.
- **Instance-store volumes are wiped on stop**, and anything in RAM is lost.
- A stopped instance usually comes back with a different public IPv4
  address, unless it has an Elastic IP.
- **Not clamped, with the reason:** an instance in an Auto Scaling group
  (the group would relaunch it; tag the group instead), a Spot instance (its
  restart depends on capacity, so the release is not guaranteed), and an
  instance that is not `running`.
- Release starts only the instances Clamper stopped.

### Auto Scaling groups

The group's `MinSize` and `DesiredCapacity` go to zero rather than its
instances being stopped, because AWS's own documentation says Auto Scaling
restarts or replaces an instance that something else stops. Stopping them
directly would look like it worked, and it would not.

- **Scaling to zero terminates the group's instances.** That is Auto
  Scaling's own behaviour, and it is why the group's sizes are what Clamper
  records and restores, not the instances.
- Anything holding state on those instances loses it. Put `clamper:protect`
  on a group whose instances are not disposable.
- Release restores the recorded min, max and desired, and Auto Scaling
  launches fresh instances from the current launch template.

### ECS services

- Running tasks are stopped. A service behind a load balancer fails its
  health checks and the target group empties.
- A service with a long draining timeout takes a while to reach zero. The
  clamp is recorded when the API accepts it, which is not the same moment
  as the tasks being gone.
- Release restores the recorded desired count.

### RDS instances and Aurora clusters

- **AWS restarts a stopped instance or cluster after seven consecutive
  days.** The scheduled reconcile notices a clamped database that is
  available again, stops it again, and says so in the notification — if a
  person started it on purpose, `clamper release` is the way to keep it up.
- Storage, provisioned IOPS and backup storage keep billing while it is
  stopped. The saving is the instance hours.
- Starting a stopped database requires recovery and can take minutes to
  hours. A release is not instant.
- **Not clamped, with the reason:** an instance that has a read replica or
  is one, an RDS for SQL Server instance in a Multi-AZ deployment, and a
  cluster in a global database. AWS refuses all three.

### Lambda functions

Reserved concurrency goes to zero, so every invocation is throttled. The
function, its code and its triggers are untouched; it is the least
destructive way to stop a function in a retry loop from costing anything.

- Throttled invocations fail. An asynchronous invocation is retried and may
  land in the function's dead-letter queue or on-failure destination; a
  synchronous caller gets a 429.
- An event source mapping keeps trying, so an SQS queue behind a clamped
  function accumulates and may start sending messages to its own
  dead-letter queue.
- Release puts the recorded reservation back. If the function had none, the
  release *removes* the setting with `lambda:DeleteFunctionConcurrency`
  rather than inventing a number — the one named exception to "no delete
  verbs", because it deletes a setting, not a resource.
- Clamper's own enforcer function is excluded three independent ways. An
  enforcer that throttled itself could not release itself, and nor could
  anything else.

### SageMaker notebook instances

- Anything on the instance's own filesystem outside
  `/home/ec2-user/SageMaker` is lost on stop, including installed packages.
  The EBS volume for that directory survives and keeps billing.
- A running notebook's kernel state is lost, and unsaved work with it.

## Deliberately not here

**Nothing that terminates or deletes.** A row in `actions.yml` whose method
or IAM action starts with a terminate, delete or other destructive verb
makes the registry refuse to load, so the package fails to import. This is a
property of the code, not a policy in a document.

**SageMaker endpoints.** They are often the biggest number on the bill, and
the only way to stop their cost is `DeleteEndpoint`. The inverse would be
`CreateEndpoint` with a configuration that may reference a model somebody
has since deleted — a rebuild that might fail, not a restore. A release
that silently fails to bring back a production inference endpoint is worse
than never having stopped it. `clamper plan` lists the endpoints it finds,
priced, under "found, not in v1", so the number stays visible.

**EKS node groups.** Scaling a managed node group to zero is reversible, but
it evicts pods with no regard for disruption budgets, orphans StatefulSet
volumes, and leaves the cluster's control plane billing anyway. It needs a
design that drains rather than scales, and is the most likely addition after
v1.

**Anything data-plane.** Clamper does not cap data transfer, does not touch
S3, and cannot stop charges already accrued.

## Adding an action

See [`CONTRIBUTING.md`](../CONTRIBUTING.md#adding-an-action). In short: a
row in `actions.yml`, a handler in `core/resources/`, a moto round-trip test,
and no change to the actuator. If the action is not reversible from what the
ledger records, it does not belong in Clamper.
