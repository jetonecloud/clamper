---
name: Bug report
about: Something doesn't work the way it should
labels: bug
---

**Please do not report security issues here.** Anything that could make
Clamper stop something it should not, fail to release something it stopped,
or report a clamp that did not happen as done goes to
security@jetonecloud.com — see [SECURITY.md](../../SECURITY.md).

## What happened

## What you expected

## Steps to reproduce

```sh
clamper ...
```

Does it reproduce with `clamper demo` or `clamper simulate`? Either one
narrows it down a lot.

## Output

Paste the terminal output. **Check it first** — a report names your
resources, your spend and what is currently stopped. Replace account ids and
resource ids if you would rather not share them; keep their shape.

```
```

## Versions

- `clamper version`:
- The version the enforcer was deployed with, if different (the stack's
  `Description` says the mode; `clamper deploy` prints the zip key):
- OS and Python version:
- Installed via: pipx / uv / pip / Docker / checkout

## Anything else

If a resource was not clamped, the line under NOT CLAMPED says why. If a
release reported a conflict, `clamper ledger <clamp-id>` shows what Clamper
set and what it was before. If a limit says "unknown", `clamper doctor`
usually says which permission or budget is missing.
