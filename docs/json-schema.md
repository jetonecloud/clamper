# JSON output

`--format json` on `plan`, `status`, `clamp`, `release`, `simulate` and
`demo` writes one JSON object. This is its contract.

Schema version: `1.0`. Additive changes — a new key, a new status — bump the
minor version. Anything that removes or renames a key, or changes a type,
bumps the major. Consumers should check `schema_version` and refuse a major
they do not know.

**Every key is always present.** A value that does not apply is `null` or an
empty list, never absent, so a missing key and a null key are not two
different questions.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `tool` | string | Always `"clamper"` |
| `tool_version` | string | The Clamper version that wrote it |
| `kind` | string | `plan`, `run`, `status` or `release` |
| `generated_at` | string | ISO 8601, UTC |
| `stack_name` | string | From `clamper.yml` |
| `mode` | string | `dry-run` or `enforce` |
| `dry_run` | bool | `mode != "enforce"`. True means **nothing was changed** |
| `actor` | string or null | Who ran it: `cli:<arn>`, `enforcer:alert`, `enforcer:schedule` |
| `account` | string or null | The AWS account id, when known |
| `regions` | list of strings | The regions searched |
| `exit_code` | int | The process exit code; see below |
| `summary` | object | Counts; see below |
| `clamp_id` | string or null | The ledger id of the run this report describes, if it opened one |
| `release_command` | string or null | The exact command to undo this run. Null unless something was really stopped |
| `limits` | list of limit objects | Every configured limit, as decided |
| `acted` | list of line objects | What was done, or would be: see statuses |
| `not_acted` | list of line objects | Opted in and **not** done, each with a reason. Still costing money |
| `active` | list of line objects | For `status`: what Clamper currently has stopped |
| `protected` | list of line objects | Every resource carrying the protect tag |
| `unsupported` | list of line objects | Found and out of v1's reach, e.g. SageMaker endpoints |
| `errors` | list of strings | Things that could not be checked. Each is a hole in the report |
| `notes` | list of strings | Caveats a reader needs |

## `summary`

Every count is computed from line statuses. None is computed from the number
of targets attempted.

| Key | Type | Meaning |
|---|---|---|
| `limits_breached` | int | Limits at or over their clamp threshold |
| `limits_unknown` | int | Limits whose spend could not be read. Never treated as fine |
| `stopped` | int | Lines with status `clamped` or `re-stopped`. **The only count of things really stopped** |
| `would_stop` | int | `would-clamp` plus `will-clamp`. Nothing was changed for these |
| `released` | int | Lines with status `released` |
| `not_done` | int | Length of `not_acted` |
| `needs_attention` | int | Lines whose status is in the attention set below |
| `protected` | int | Length of `protected` |
| `not_selected` | int | Resources found and never opted in. Counted, not listed |
| `over_cap` | bool | Targets exceeded `max_actions_per_run`, so nothing was clamped |
| `cap` | int | `max_actions_per_run` at the time |
| `indicative_hourly_usd` | number or null | Sum of the priced lines in `acted`. Indicative list prices, not a bill |

## Limit object

`id`, `amount_usd`, `scope` (a label), `level` (`ok`, `warn`, `clamp` or
`unknown`), `actual_usd` and `forecast_usd` (number or null — a null
forecast means AWS has not produced one yet, not zero), `percent` (of the
limit, on `basis`), `basis` (`actual` or `forecasted`), `reason`,
`warn_percent`, `clamp_percent`, and `mismatch` (a sentence if the deployed
budget's amount disagrees with `clamper.yml`, else null).

## Line object

`status`, `action_id`, `title`, `target_id`, `region`, `description`,
`detail` (the reason, the error, or what was restored), `hourly_usd` (number
or null — null means the price table has no figure, not zero),
`price_basis`, `clamp_id`.

Tag values never appear in any line.

### Statuses

| Status | In | Meaning |
|---|---|---|
| `will-clamp` | `acted` | `plan`: would be clamped if a limit were breached |
| `would-clamp` | `acted` | Dry-run: would have been clamped. Nothing was changed |
| `clamped` | `acted` | Stopped. AWS accepted the call |
| `re-stopped` | `acted` | AWS restarted it by itself (RDS after seven days) and it was stopped again |
| `released` | `acted` | Put back as recorded |
| `would-release` | `acted` | Dry-run release |
| `failed` | `not_acted` | AWS refused the call. `detail` has its error |
| `pending` | `not_acted` | **Outcome unknown**: a run stopped between recording and confirming |
| `held` | `not_acted` | Over the cap; nothing was clamped |
| `skipped` | `not_acted` | Not clampable now: already stopped, a read replica, out of scope... |
| `excluded` | `not_acted` | Clamper's own resource |
| `protected` | `protected` | Carries the protect tag |
| `unsupported` | `unsupported` | Found, and v1 has no reversible action for it |
| `conflict` | `not_acted` | Release: changed by hand since the clamp, left alone |
| `gone` | `not_acted` | Release: the resource no longer exists |
| `unrestorable` | `not_acted` | Release: the ledger entry failed validation |
| `release-failed` | `not_acted` | Release: AWS refused the call |
| `error` | `not_acted` | The resource's state could not be read |

The **attention set** — `failed`, `pending`, `conflict`, `gone`,
`unrestorable`, `release-failed`, `error` — makes the exit code 2.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing to do |
| 1 | A limit is breached, something is clamped, or targets are held over the cap |
| 2 | Something could not be checked or done. 2 beats 1 |
