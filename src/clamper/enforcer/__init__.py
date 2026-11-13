"""The half that runs in the user's account.

A single small Lambda, deployed by `clamper deploy`, triggered two ways:

- an SNS message from the AWS Budgets notification the stack creates, and
- an EventBridge schedule, every six hours by default.

Both do the same thing: ask Budgets what the spend is, decide, record, act.
The schedule is not a nicety. AWS sends an `ACTUAL` budget alert **once per
budget per budget period**, so a design that only listened to SNS would get
exactly one chance per month and no second opinion if the Lambda was being
deployed at that moment.

Keep this package's imports to the standard library, `boto3` and
`clamper.core`. `rich` and `typer` are not in the deployment zip and a
module here that imports one will fail at cold start, not in CI — so
`tests/test_safety.py` checks it instead.
"""
