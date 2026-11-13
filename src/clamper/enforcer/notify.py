"""Send the notification: to the SNS topic always, to Slack if configured.

Two writes to Clamper's own infrastructure, pinned in
`tests/test_safety.py`: `sns:Publish` to the stack's notifications topic, and
an HTTPS POST to a Slack webhook. Neither touches a resource anybody is
paying for.

**The webhook URL is a credential.** Anyone holding it can post to that
channel as you. So it lives in SSM Parameter Store as a SecureString, the
config names only the parameter, and the URL is read at send time and held
in a local variable for the length of one request. It is never logged,
never written to the ledger, and never in a template parameter, which
anybody with `cloudformation:DescribeStacks` can read.

A notification that fails to send never fails the run. The clamp has
already happened and been recorded; the ledger and `clamper status` are the
source of truth, and the failure is logged so the next person to look sees
it.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError

from clamper.core.report import model, notification
from clamper.core.session import Clients

log = logging.getLogger("clamper.enforcer")

#: The only host a webhook may point at. A parameter somebody could change to
#: point anywhere would make the enforcer a way to send the notification —
#: resource ids and spend — to any URL.
SLACK_HOST = "hooks.slack.com"

SLACK_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class Delivery:
    sns: bool
    slack: bool | None
    errors: tuple[str, ...] = ()


def send(
    report: model.Report,
    *,
    clients: Clients,
    topic_arn: str,
    slack_parameter: str | None = None,
) -> Delivery:
    subject = notification.subject(report)
    text = notification.body(report)
    errors: list[str] = []

    sns_ok = False
    sns_client = clients.get("sns")
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=text)
        sns_ok = True
    except (ClientError, BotoCoreError) as exc:
        errors.append(f"sns: {type(exc).__name__}")

    slack_ok: bool | None = None
    if slack_parameter:
        slack_ok = False
        try:
            _post_to_slack(clients, slack_parameter, f"*{subject}*\n```\n{text}```")
            slack_ok = True
        except (ClientError, BotoCoreError, OSError, ValueError) as exc:
            # The exception type only. A message from urllib can quote the
            # URL, and the URL is the credential.
            errors.append(f"slack: {type(exc).__name__}")

    for error in errors:
        log.warning("notification not delivered: %s", error)
    return Delivery(sns=sns_ok, slack=slack_ok, errors=tuple(errors))


def _post_to_slack(clients: Clients, parameter: str, text: str) -> None:
    ssm_client = clients.get("ssm")
    response = ssm_client.get_parameter(Name=parameter, WithDecryption=True)
    url = str(response.get("Parameter", {}).get("Value") or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != SLACK_HOST:
        raise ValueError("the Slack webhook parameter does not hold an https://hooks.slack.com URL")
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS) as reply:  # noqa: S310
        reply.read()
