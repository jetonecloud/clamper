"""AWS credentials and clients, in one place.

Two jobs, both deliberately narrow.

**Credentials.** Clamper never accepts an access key. There is no
`--access-key` flag, no prompt, no config key, and nothing is written to
disk. `boto3.Session` resolves credentials the way every other AWS tool does
— environment variables, `~/.aws/config`, SSO, a container or instance role,
`AWS_PROFILE` — and `--role-arn` assumes a role *from* whatever that chain
produced. A tool that accepts a pasted secret key teaches people to paste
secret keys, and the ones they paste end up in shell history and CI logs.

**Clients.** This is the only module in the package that constructs a boto3
client. `tests/test_safety.py` asserts that structurally, because the point
of having one write path is undone by a second module that can make its own
client and call whatever it likes on it.

Inside the Lambda there is no profile and no role to assume: the execution
role is already the identity, so `build_session()` with no arguments is the
whole of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

#: How long an assumed-role session lasts. Short: a clamp takes seconds, and
#: a longer session is a longer window for a leaked token to matter.
SESSION_DURATION_SECONDS = 3600

SESSION_NAME = "clamper"

#: Retries matter more here than in a read-only tool. A throttled
#: `StopInstances` that is not retried is a resource that keeps billing, and
#: a clamp run touching fifty resources will hit a rate limit somewhere.
#: `adaptive` because the standard mode's fixed backoff is the wrong shape
#: for a burst of writes against one service.
RETRY_CONFIG = BotoConfig(
    retries={"max_attempts": 5, "mode": "adaptive"},
    user_agent_extra="clamper",
)

#: Budgets is a global service with its endpoint in us-east-1, whatever
#: region the rest of the account lives in. Getting this wrong produces an
#: endpoint error rather than an empty result, which is at least loud.
BUDGETS_REGION = "us-east-1"

#: Cost Explorer is the same, and is used for exactly one free call in
#: `doctor` — `ce:ListCostAllocationTags`. See docs/design.md for why
#: `ce:GetCostAndUsage` is not used anywhere.
COST_EXPLORER_REGION = "us-east-1"

#: Services whose endpoint does not follow the caller's region.
GLOBAL_SERVICES = {
    "budgets": BUDGETS_REGION,
    "ce": COST_EXPLORER_REGION,
}


class CredentialsError(RuntimeError):
    """Credentials are missing, expired, or cannot assume the role asked for.

    Carries a `reason` so `doctor` can report precisely which it was — "no
    credentials" and "expired credentials" need different fixes and a user
    should not have to guess which one they have.
    """

    def __init__(self, message: str, *, reason: str = "credentials_unavailable") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Identity:
    """Who we are, as STS reports it."""

    account: str
    arn: str
    user_id: str

    @property
    def is_root(self) -> bool:
        """The account root user. Never the right thing to run this as."""
        return self.arn.endswith(":root")

    @property
    def label(self) -> str:
        return f"{self.arn} (account {self.account})"


def build_session(
    *,
    profile: str | None = None,
    region: str | None = None,
    role_arn: str | None = None,
) -> Any:
    """A `boto3.Session` from the standard chain, optionally assuming a role.

    Raises `CredentialsError` with a specific reason rather than letting a
    botocore exception out, because every caller wants to say what went wrong
    rather than print a traceback.
    """
    try:
        base = boto3.Session(profile_name=profile, region_name=region)
    except BotoCoreError as exc:
        raise CredentialsError(
            f"Could not build an AWS session: {exc}", reason="credentials_unavailable"
        ) from exc

    if role_arn is None:
        return base

    try:
        assumed = base.client("sts", config=RETRY_CONFIG).assume_role(
            RoleArn=role_arn,
            RoleSessionName=SESSION_NAME,
            DurationSeconds=SESSION_DURATION_SECONDS,
        )
    except ClientError as exc:
        code = _code(exc)
        raise CredentialsError(
            f"Could not assume {role_arn} ({code}).", reason=_reason_for(code)
        ) from exc
    except BotoCoreError as exc:
        raise CredentialsError(
            f"Could not assume {role_arn}: {exc}", reason="credentials_unavailable"
        ) from exc

    credentials = assumed["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region or base.region_name,
    )


class Clients:
    """Cached boto3 clients, per service and region.

    The only client factory in the package. Clients are cached because a
    clamp run makes several calls per service per region and client
    construction parses a service model each time, which is the single
    slowest thing a short-lived Lambda does.

    `stub` exists for the tests: botocore's `Stubber` needs the client it is
    stubbing, and handing one in here is how a test drives the real actuator
    against a validated fake rather than a mock that would accept anything.
    """

    def __init__(self, session: Any, *, default_region: str | None = None) -> None:
        self._session = session
        self._default_region = default_region or getattr(session, "region_name", None)
        self._cache: dict[tuple[str, str | None], Any] = {}

    @property
    def default_region(self) -> str | None:
        return self._default_region

    def get(self, service: str, *, region: str | None = None) -> Any:
        """A client for `service`, in `region` or the session's default."""
        where = GLOBAL_SERVICES.get(service, region or self._default_region)
        key = (service, where)
        if key not in self._cache:
            self._cache[key] = self._session.client(service, region_name=where, config=RETRY_CONFIG)
        return self._cache[key]

    def stub(self, service: str, client: Any, *, region: str | None = None) -> None:
        """Pre-seed the cache with a client a test already holds."""
        where = GLOBAL_SERVICES.get(service, region or self._default_region)
        self._cache[(service, where)] = client


def whoami(session: Any) -> Identity:
    """Ask STS who these credentials belong to.

    The cheapest possible check that credentials work at all, which is what
    `doctor` uses it for, and the source of the account id every ARN in the
    generated policy is scoped to.
    """
    try:
        response = session.client("sts", config=RETRY_CONFIG).get_caller_identity()
    except ClientError as exc:
        code = _code(exc)
        raise CredentialsError(
            f"AWS rejected these credentials ({code}).", reason=_reason_for(code)
        ) from exc
    except BotoCoreError as exc:
        raise CredentialsError(
            f"No usable AWS credentials found: {exc}", reason="credentials_unavailable"
        ) from exc

    return Identity(
        account=str(response.get("Account") or ""),
        arn=str(response.get("Arn") or ""),
        user_id=str(response.get("UserId") or ""),
    )


def _code(exc: ClientError) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "")
    return type(exc).__name__


def _reason_for(code: str) -> str:
    if code in {"ExpiredToken", "ExpiredTokenException", "TokenRefreshRequired"}:
        return "credentials_expired"
    if code in {"InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch"}:
        return "credentials_invalid"
    if code in {"AccessDenied", "AccessDeniedException"}:
        return "credentials_denied"
    return "credentials_unavailable"
