"""`clamper demo`: a plan and a clamp run, with no AWS account at all.

The point of a demo is that it shows the real thing. So this runs the real
`build_plan()` and the real `reconcile()` — selection, the cap, self-
exclusion, the write-ahead ledger, the actuator, the report — against three
stand-ins, all built from the fixtures in `data/demo/`:

- **spend**, in place of `DescribeBudget`;
- **discovery**, in place of the handlers' `Describe*` calls;
- **an account** that answers the actuator's calls: it accepts every one
  except those the fixture says to refuse, and remembers what it was asked.

Nothing here constructs a boto3 client or makes a network call. The
`Clients` the actuator uses is the real class, handed a session whose
`client()` returns the fake account, so the dispatch being demonstrated is
the dispatch that will run.

Not in the Lambda zip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Any

import yaml
from botocore.exceptions import ClientError

from clamper.core import config as config_module
from clamper.core.actions import Action, load_registry
from clamper.core.actuator import Actuator
from clamper.core.budgets import Spend
from clamper.core.config import Config, Limit
from clamper.core.cost import Price, load_table
from clamper.core.discover import Discovery
from clamper.core.ledger import MemoryLedger
from clamper.core.plan import build_plan
from clamper.core.report import model
from clamper.core.resources import Candidate, Unsupported, handler_for
from clamper.core.session import Clients
from clamper.core.targets import SelfIdentity
from clamper.enforcer.run import TRIGGER_ALERT, Outcome, reconcile

REGION = "us-east-1"
ACTOR = "demo"

DEMO_NOTE = (
    "This is `clamper demo`: a made-up account from bundled fixtures. Nothing was read from "
    "or sent to AWS. The exit line is what a real run would exit with; the demo itself "
    "always exits 0."
)


@dataclass
class DemoAccount:
    """Answers the actuator's calls. Accepts all of them, bar `refuse`."""

    refuse: Mapping[str, Mapping[str, str]]
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


class _Service:
    """One service of the fake account. Any method is a call it records."""

    def __init__(self, account: DemoAccount, service: str) -> None:
        self._account = account
        self._service = service

    def __getattr__(self, method: str) -> Callable[..., dict[str, Any]]:
        account = self._account
        service = self._service

        def call(**params: Any) -> dict[str, Any]:
            account.calls.append((service, method, dict(params)))
            for value in _identifiers(params):
                refused = account.refuse.get(value)
                if refused:
                    raise ClientError(
                        {"Error": {"Code": refused["code"], "Message": refused["message"]}},
                        method,
                    )
            return {}

        return call


class _Session:
    """What `Clients` asks for clients. `region_name` is all it reads."""

    region_name = REGION

    def __init__(self, account: DemoAccount) -> None:
        self._account = account

    def client(self, service: str, **_: Any) -> _Service:
        return _Service(self._account, service)


@dataclass(frozen=True)
class Demo:
    config: Config
    plan: model.Report
    run: Outcome
    account: DemoAccount


def run_demo() -> Demo:
    fixtures = _fixtures()
    text = resources.files("clamper.data.demo").joinpath("clamper.yml").read_text("utf-8")
    config = config_module.loads(text, source="clamper demo")
    registry = load_registry()
    candidates, unsupported = _discovered(fixtures)
    identity = SelfIdentity(config.stack_name)
    read_spend = _spend(fixtures)
    account_id = str(fixtures["account"])

    def find(actions: tuple[Action, ...] | None = None) -> Discovery:
        wanted = {action.id for action in actions} if actions is not None else None
        return Discovery(
            candidates=tuple(
                item for item in candidates if wanted is None or item.action_id in wanted
            ),
            unsupported=unsupported if actions is None else (),
            regions=(REGION,),
        )

    plan = build_plan(
        config,
        read_spend=read_spend,
        find=find,
        identity=identity,
        actor=ACTOR,
        account=account_id,
    )

    account = DemoAccount(refuse=fixtures.get("refuse") or {})
    clients = Clients(_Session(account), default_region=REGION)
    outcome = reconcile(
        config,
        actuator=Actuator(clients, dry_run=config.dry_run, registry=registry),
        ledger=MemoryLedger(),
        identity=identity,
        read_spend=read_spend,
        find=find,
        live_state=lambda entry: None,
        actor=ACTOR,
        trigger=TRIGGER_ALERT,
        account=account_id,
        registry=registry,
    )
    return Demo(
        config=config,
        plan=_noted(plan),
        run=replace(outcome, report=_noted(outcome.report)),
        account=account,
    )


def _noted(report: model.Report) -> model.Report:
    return replace(report, notes=(DEMO_NOTE, *report.notes))


# --- fixtures -----------------------------------------------------------


def _fixtures() -> dict[str, Any]:
    text = resources.files("clamper.data.demo").joinpath("account.yml").read_text("utf-8")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("data/demo/account.yml is not a mapping")
    return loaded


def _spend(fixtures: Mapping[str, Any]) -> Callable[[Limit], Spend]:
    figures = fixtures.get("spend") or {}

    def read(limit: Limit) -> Spend:
        item = figures.get(limit.id) or {}
        return Spend(
            limit=limit,
            budget_name=limit.budget_name("clamper"),
            actual=item.get("actual"),
            forecast=item.get("forecast"),
            budget_amount=item.get("budget"),
            error=None if item.get("actual") is not None else "no figure in the demo fixture",
        )

    return read


def _discovered(
    fixtures: Mapping[str, Any],
) -> tuple[tuple[Candidate, ...], tuple[Unsupported, ...]]:
    registry = load_registry()
    candidates: list[Candidate] = []
    for item in fixtures.get("resources") or []:
        handler = handler_for(registry.get(str(item["action"])))
        target_id = str(item["id"])
        description = str(item.get("description") or "")
        tags = {str(key): str(value) for key, value in (item.get("tags") or {}).items()}
        price = _price(item.get("price"))
        if item.get("unclampable"):
            candidates.append(
                handler.unclampable(
                    target_id=target_id,
                    region=REGION,
                    description=description,
                    tags=tags,
                    reason=str(item["unclampable"]),
                    price=price,
                )
            )
        else:
            candidates.append(
                handler.candidate(
                    target_id=target_id,
                    region=REGION,
                    description=description,
                    tags=tags,
                    state=dict(item.get("state") or {}),
                    price=price,
                )
            )
    unsupported = tuple(
        Unsupported(
            kind=str(item["kind"]),
            resource_id=str(item["id"]),
            region=REGION,
            description=str(item.get("description") or ""),
            reason=str(item["reason"]),
            price=_price(item.get("price")),
        )
        for item in fixtures.get("unsupported") or []
    )
    return tuple(candidates), unsupported


def _price(spec: Mapping[str, Any] | None) -> Price | None:
    if not spec:
        return None
    return load_table().price(
        str(spec["family"]), str(spec["type"]), multiplier=float(spec.get("count") or 1)
    )


def _identifiers(params: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    for value in params.values():
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(item) for item in value)
    return found
