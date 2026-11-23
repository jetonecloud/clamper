"""The price table's per-family helpers."""

from __future__ import annotations

import pytest

from clamper.core.cost import load_table, redshift_price


def test_a_redshift_cluster_is_priced_per_node() -> None:
    price = redshift_price("ra3.xlplus", 2)
    assert price is not None
    assert price.hourly_usd == pytest.approx(2 * load_table().families["redshift"]["ra3.xlplus"])
    assert price.basis.startswith("2x ra3.xlplus, ")


def test_an_unlisted_node_type_gets_no_figure() -> None:
    assert redshift_price("ra9.huge", 4) is None


@pytest.mark.parametrize("nodes", [0, -1])
def test_a_nonsense_node_count_gets_no_figure(nodes: int) -> None:
    assert redshift_price("dc2.large", nodes) is None
