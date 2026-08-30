"""Tests for exporters.populations: Population, resolve_populations.

Sidecars are built directly via `Sidecar.from_raw` (resolution is
sidecar-only, no DuckDB needed), mirroring test_election.py's fixture style.
"""

from __future__ import annotations

import pytest
from _support.sidecar_builder import enum_options

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.errors import (
    SourceSubTypesOnFlatKind,
    SourceTableKindUnknown,
    SourceTableSubTypeUnknown,
)
from fabulexa_forge.exporters.populations import Population, resolve_populations
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(name: str, type_: str = "VARCHAR") -> dict[str, object]:
    return {"name": name, "type": type_}


def _records_table(kind: str, discriminator: bool = False) -> dict[str, object]:
    """Build a raw records__<kind> table entry.

    `discriminator=True` adds a `prop__<kind>_type` column, mirroring a
    sub-typed kind's declared discriminator (the domain itself lives in
    `enum_domains`, consulted independently by `subtype_values`).
    """
    cols = [_col("fork_path"), _col("record_id")]
    if discriminator:
        cols.append(_col(f"prop__{kind}_type"))
    return {
        "name": f"records__{kind}",
        "category": "records",
        "record_kind": kind,
        "columns": cols,
        "rows": 1,
    }


def _sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, object] | None = None,
) -> Sidecar:
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = {
            kind: {prop: enum_options(*values) for prop, values in props.items()}
            for kind, props in enum_domains.items()
        }
    return Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# Flat kind
# ---------------------------------------------------------------------------


def test_flat_kind_resolves_single_atom() -> None:
    """A flat kind (no discriminator domain) resolves to (kind, None)."""
    sidecar = _sidecar([_records_table("trip")])
    result = resolve_populations(sidecar, "table 'trips'", "trip", None)
    assert result == (Population(kind="trip", sub_type=None),)


# ---------------------------------------------------------------------------
# Sub-typed kind, no explicit sub_types
# ---------------------------------------------------------------------------


def test_subtyped_kind_without_sub_types_resolves_full_domain() -> None:
    """No `sub_types` given: resolves every declared sub-type, domain order."""
    sidecar = _sidecar(
        [_records_table("customer", discriminator=True)],
        enum_domains={"customer": {"customer_type": ["standard", "vip", "trial"]}},
    )
    result = resolve_populations(sidecar, "table 'customers'", "customer", None)
    assert result == (
        Population(kind="customer", sub_type="standard"),
        Population(kind="customer", sub_type="vip"),
        Population(kind="customer", sub_type="trial"),
    )


# ---------------------------------------------------------------------------
# Sub-typed kind, explicit sub_types out of domain order
# ---------------------------------------------------------------------------


def test_explicit_sub_types_resolve_in_domain_order() -> None:
    """Explicit `sub_types` given out of domain order still resolve in
    domain declaration order, not the given order."""
    sidecar = _sidecar(
        [_records_table("customer", discriminator=True)],
        enum_domains={"customer": {"customer_type": ["standard", "vip", "trial"]}},
    )
    result = resolve_populations(
        sidecar, "table 'vip_and_standard'", "customer", ("vip", "standard")
    )
    assert result == (
        Population(kind="customer", sub_type="standard"),
        Population(kind="customer", sub_type="vip"),
    )


# ---------------------------------------------------------------------------
# Resolution errors
# ---------------------------------------------------------------------------


def test_unknown_kind_raises_source_table_kind_unknown() -> None:
    """A declared kind with no records__<kind> table -> SourceTableKindUnknown,
    message prefixed with the verbatim owner label."""
    sidecar = _sidecar([_records_table("trip")])
    with pytest.raises(SourceTableKindUnknown, match=r"table 'trips': kind 'ghost'"):
        resolve_populations(sidecar, "table 'trips'", "ghost", None)


def test_sub_type_outside_domain_raises_source_table_sub_type_unknown() -> None:
    """A sub_types entry outside the discriminator domain ->
    SourceTableSubTypeUnknown; the 'events source #2' owner form appears
    verbatim."""
    sidecar = _sidecar(
        [_records_table("customer", discriminator=True)],
        enum_domains={"customer": {"customer_type": ["standard", "vip"]}},
    )
    with pytest.raises(
        SourceTableSubTypeUnknown,
        match=r"events source #2: sub_type 'enterprise' not declared for kind 'customer'",
    ):
        resolve_populations(
            sidecar, "events source #2", "customer", ("standard", "enterprise")
        )


def test_sub_types_on_flat_kind_raises_source_sub_types_on_flat_kind() -> None:
    """sub_types given for a flat kind -> SourceSubTypesOnFlatKind."""
    sidecar = _sidecar([_records_table("trip")])
    with pytest.raises(SourceSubTypesOnFlatKind, match=r"table 'trips'"):
        resolve_populations(sidecar, "table 'trips'", "trip", ("express",))


def test_every_population_appears_exactly_once() -> None:
    """The full discriminator domain resolves with no duplicates."""
    sidecar = _sidecar(
        [_records_table("customer", discriminator=True)],
        enum_domains={"customer": {"customer_type": ["a", "b", "c"]}},
    )
    result = resolve_populations(sidecar, "table 'customers'", "customer", None)
    assert len(result) == len(set(result)) == 3
