"""Data files, loaded through `importlib.resources`.

- `actions.yml` — every write Clamper can make, and how to undo it.
- `prices.yml` — the per-hour figures `clamper plan` shows.
- `clamper.starter.yml` — what `clamper init` writes.
- `demo/` — fixtures for `clamper demo`, which needs no AWS account.

These are data rather than code so that adding a price or reading the list
of possible writes needs no Python.
"""
