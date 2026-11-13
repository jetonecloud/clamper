"""The parts that decide and the one part that acts.

Everything here is importable inside a Lambda: no `typer`, no `rich`, no
terminal. The CLI and the enforcer are two front ends over this package, so
the rules about what may be clamped are written once and both halves agree
by construction.

The module layout is the safety design:

- `actions.py` loads the declarations. Nothing else decides what a valid
  write is.
- `actuator.py` is the only module that performs one. Everything else
  returns a `Call` for it to check and make.
- `ledger.py` records the previous state before the call happens.
"""
