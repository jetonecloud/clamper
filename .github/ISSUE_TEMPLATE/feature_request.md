---
name: Feature request
about: Something Clamper should do and doesn't
labels: enhancement
---

## What you're trying to do

The problem, rather than the solution — it often turns out there's a simpler
answer than the one that brought you here.

## What you'd want it to do

## If it is a new resource type

The two questions that decide whether it belongs:

- **Can it be released from what is recorded?** Every action in Clamper has
  an inverse that restores the previous state exactly. "Delete it and
  recreate it later" is not a release.
- **What does a clamp do to somebody at 3am?** Lost data, dropped
  connections, a queue filling up — say what you know.

[`docs/actions.md`](../../docs/actions.md) shows how the existing seven
answer both.

## Out of scope for v1

These are deliberate omissions rather than oversights, so an issue asking for
one will probably be closed with a link to this list:

- **Anything that terminates or deletes.** The registry refuses to load it.
- **SageMaker endpoints.** The only way to stop their cost is to delete them,
  and the release would be a rebuild that might fail.
- **EKS node groups.** Reversible, but scaling to zero ignores pod disruption
  budgets. It needs a design that drains; it is the most likely v2 addition.
- **Data-plane limits** — data transfer, S3 — and stopping money already
  spent.
- **Cent-accurate limits.** Budgets data refreshes up to three times a day,
  and nothing built on it can be faster than that.
- A dashboard, a hosted service, multi-account orchestration, other clouds.
