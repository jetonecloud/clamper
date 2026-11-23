"""Per-hour figures for `clamper plan`, from a static table.

The job is **ranking**, not billing. `clamper plan` has to put the
expensive thing at the top of the page, because the person reading it during
a cost incident has thirty seconds and the first line is the one they act
on. For that, an indicative figure is worth a great deal and a precise one
is worth barely more.

So, in the spirit of Preflight's "no number you cannot defend":

- Figures come from [`prices.yml`](../data/prices.yml), a static table of
  us-east-1 on-demand list prices. They are **indicative**, carry a `basis`
  sentence, and are rendered with a `~`.
- An instance type that is not in the table gets **no figure at all**, and
  the plan says "price unknown" rather than guessing from the family. A
  wrong number in a report to a manager costs more credibility than the
  number was worth.
- Savings Plans, Reserved Instances, regional differences, Multi-AZ
  surcharges other than RDS's 2x, EBS, snapshots and data transfer are not
  modelled. Every report says so.
- Nothing here is ever used to decide whether to clamp. The decision comes
  from the budget; this only affects the order of the list.

Clamper makes no pricing API call. `pricing:GetProducts` would be a live
figure and a per-request cost, and the design's whole claim is that Clamper
is not a cost of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

DATA_PACKAGE = "clamper.data"
DATA_FILE = "prices.yml"

HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class Price:
    """One indicative figure, and the sentence that justifies it."""

    hourly_usd: float
    basis: str

    @property
    def monthly_usd(self) -> float:
        """730 hours. Not 720, not 744: the figure AWS's own calculator uses."""
        return self.hourly_usd * HOURS_PER_MONTH

    def label(self) -> str:
        return f"~${self.hourly_usd:.3f}/hr (~${self.monthly_usd:,.0f}/mo)"


@dataclass(frozen=True)
class Table:
    version: int
    basis: str
    note: str
    families: dict[str, dict[str, float]]

    def price(self, family: str, instance_type: str, *, multiplier: float = 1.0) -> Price | None:
        """The figure for one instance type, or `None` if it is not listed."""
        rates = self.families.get(family, {})
        rate = rates.get(instance_type)
        if rate is None:
            return None
        return Price(
            hourly_usd=round(rate * multiplier, 6), basis=self._basis(instance_type, multiplier)
        )

    def _basis(self, instance_type: str, multiplier: float) -> str:
        if multiplier != 1.0:
            return f"{instance_type} at {multiplier:g}x, {self.basis}"
        return f"{instance_type}, {self.basis}"


@lru_cache(maxsize=1)
def load_table() -> Table:
    text = resources.files(DATA_PACKAGE).joinpath(DATA_FILE).read_text(encoding="utf-8")
    return loads(text)


def loads(text: str) -> Table:
    raw: Any = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("prices.yml should contain a mapping.")
    if raw.get("version") != 1:
        raise ValueError(f"prices.yml version must be 1, got {raw.get('version')!r}.")
    basis = str(raw.get("basis") or "").strip()
    if not basis:
        raise ValueError(
            "prices.yml has no `basis`. Every figure Clamper shows carries the "
            "sentence that justifies it, so a table without one cannot be used."
        )
    families: dict[str, dict[str, float]] = {}
    for name, rates in (raw.get("families") or {}).items():
        if not isinstance(rates, dict):
            raise ValueError(f"prices.yml family {name!r} should be a mapping.")
        families[str(name)] = {str(key): float(value) for key, value in rates.items()}
    return Table(
        version=1,
        basis=basis,
        note=str(raw.get("note") or ""),
        families=families,
    )


def ec2_price(instance_type: str) -> Price | None:
    return load_table().price("ec2", instance_type)


def rds_price(instance_class: str, *, multi_az: bool = False) -> Price | None:
    """RDS, with Multi-AZ as a 2x multiplier.

    The table is single-AZ PostgreSQL. Multi-AZ is two instances, so the
    multiplier is exact for the instance-hour component — which is the only
    component a stop saves. Other engines differ (SQL Server and Oracle carry
    licence costs), so their real figure is higher than this one, never lower.
    """
    return load_table().price("rds", instance_class, multiplier=2.0 if multi_az else 1.0)


def sagemaker_price(instance_type: str) -> Price | None:
    return load_table().price("sagemaker", instance_type)


def redshift_price(node_type: str, nodes: int) -> Price | None:
    """A provisioned cluster: the per-node rate times the node count.

    `None` for an unlisted node type, and for a node count below one, which
    DescribeClusters should never report and which a figure would only hide.
    """
    if nodes < 1:
        return None
    table = load_table()
    unit = table.price("redshift", node_type)
    if unit is None:
        return None
    return Price(
        hourly_usd=round(unit.hourly_usd * nodes, 6),
        basis=f"{nodes}x {node_type}, {table.basis}",
    )
