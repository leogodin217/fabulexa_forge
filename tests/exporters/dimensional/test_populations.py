"""Tests for the dimensional key-election population resolution
(`exporters/dimensional/populations.py`): `resolve_dim_source_populations`
(the destination dim's source population set, from its kind + filter) and
`resolve_fk_surface` (one FK edge's resolved surface — explicit override or
population-set inheritance), plus the two small dispatch helpers
`dim_population_sub_types` and `dim_key_projects_surface`.

Sidecar-only (no DuckDB): every function under test is a pure function of
(sidecar, arguments), so every gate test builds a sidecar directly via
`Sidecar.from_raw`, mirroring `tests/exporters/test_election.py`'s style.
"""

from __future__ import annotations

import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import ColumnDecl, TableDecl
from fabulexa_forge.errors import ElectionInheritanceAmbiguous, ExportError
from fabulexa_forge.exporters.dimensional.populations import (
    DimSourcePopulations,
    dim_key_projects_surface,
    dim_population_sub_types,
    resolve_dim_source_populations,
    resolve_fk_surface,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(name: str, type_: str = "VARCHAR") -> dict[str, object]:
    return {"name": name, "type": type_}


def _records_table(kind: str, discriminator: bool = False) -> dict[str, object]:
    """Build a raw records__<kind> table entry.

    `discriminator=True` adds a `prop__<kind>_type` column; the domain itself
    lives in `enum_domains`, consulted independently by `subtype_values`.
    """
    cols = [
        _col("fork_path"),
        _col("record_id"),
        _col("presentation_id"),
        _col("created_sim_time", "BIGINT"),
        _col("active", "BOOLEAN"),
        _col("deactivated_at", "BIGINT"),
        _col("last_mutation_sim_time", "BIGINT"),
    ]
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
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    if presentation_keys is not None:
        raw["presentation_keys"] = presentation_keys
    return Sidecar.from_raw(raw)


_ENTITY_DOMAIN: dict[str, object] = {
    "entity": {"entity_type": ["alpha", "beta", "gamma"]}
}


def _entity_sidecar() -> Sidecar:
    """A sub-typed `entity` kind (alpha/beta/gamma) plus a flat `widget` kind."""
    return _sidecar(
        [_records_table("entity", discriminator=True), _records_table("widget")],
        enum_domains=_ENTITY_DOMAIN,
    )


def _record_index_only_keys(sub_types: tuple[str, ...]) -> dict[str, object]:
    """A `keys` block electing record_index for every given sub_type — no
    presentation_keys registry required."""
    return {"entity": {sub_type: "record_index" for sub_type in sub_types}}


# ---------------------------------------------------------------------------
# resolve_dim_source_populations
# ---------------------------------------------------------------------------


class TestResolveDimSourcePopulations:
    def test_flat_kind_returns_none_singleton(self) -> None:
        sidecar = _entity_sidecar()
        result = resolve_dim_source_populations(sidecar, "widget", None)
        assert result == DimSourcePopulations(
            kind="widget", populations=(None,), proper_subset=False
        )

    def test_discriminator_conjunct_selects_singleton_proper_subset(self) -> None:
        sidecar = _entity_sidecar()
        result = resolve_dim_source_populations(
            sidecar, "entity", {"prop__entity_type": "alpha"}
        )
        assert result == DimSourcePopulations(
            kind="entity", populations=("alpha",), proper_subset=True
        )

    def test_no_conjunct_on_sub_typed_kind_is_full_domain(self) -> None:
        sidecar = _entity_sidecar()
        result = resolve_dim_source_populations(sidecar, "entity", None)
        assert result == DimSourcePopulations(
            kind="entity",
            populations=("alpha", "beta", "gamma"),
            proper_subset=False,
        )
        # An empty filter dict carries no conjunct either.
        assert resolve_dim_source_populations(sidecar, "entity", {}) == result

    def test_discriminator_conjunct_on_non_sub_typed_kind_is_ordinary_conjunct(
        self,
    ) -> None:
        """`widget` has no declared discriminator domain — a
        `prop__widget_type` filter conjunct addresses no population set and
        the flat-kind whole-table population is returned unconditionally."""
        sidecar = _entity_sidecar()
        result = resolve_dim_source_populations(
            sidecar, "widget", {"prop__widget_type": "anything"}
        )
        assert result == DimSourcePopulations(
            kind="widget", populations=(None,), proper_subset=False
        )

    def test_out_of_domain_conjunct_value_raises(self) -> None:
        sidecar = _entity_sidecar()
        with pytest.raises(ExportError, match="not a declared sub-type"):
            resolve_dim_source_populations(
                sidecar, "entity", {"prop__entity_type": "delta"}
            )

    def test_non_string_conjunct_value_raises(self) -> None:
        sidecar = _entity_sidecar()
        with pytest.raises(ExportError, match="not a declared sub-type"):
            resolve_dim_source_populations(sidecar, "entity", {"prop__entity_type": 7})


# ---------------------------------------------------------------------------
# resolve_fk_surface
# ---------------------------------------------------------------------------


class TestResolveFkSurface:
    def test_explicit_override_wins_over_any_election(self) -> None:
        sidecar = _entity_sidecar()
        election = resolve_election(sidecar, _record_index_only_keys(("alpha",)))
        dim_populations = DimSourcePopulations(
            kind="entity", populations=("alpha",), proper_subset=True
        )
        resolved = resolve_fk_surface(
            election, dim_populations, "record_id", "fact.entity_id"
        )
        assert resolved == "record_id"

    def test_inherits_over_a_one_election_set(self) -> None:
        sidecar = _entity_sidecar()
        election = resolve_election(sidecar, _record_index_only_keys(("alpha", "beta")))
        dim_populations = DimSourcePopulations(
            kind="entity", populations=("alpha", "beta"), proper_subset=False
        )
        resolved = resolve_fk_surface(election, dim_populations, None, "fact.entity_id")
        assert resolved == "record_index"

    def test_mixed_set_without_override_raises_inheritance_ambiguous(self) -> None:
        sidecar = _entity_sidecar()
        election = resolve_election(
            sidecar, {"entity": {"alpha": "record_index", "beta": "record_id"}}
        )
        dim_populations = DimSourcePopulations(
            kind="entity", populations=("alpha", "beta"), proper_subset=False
        )
        with pytest.raises(ElectionInheritanceAmbiguous) as exc_info:
            resolve_fk_surface(election, dim_populations, None, "fact.entity_id")
        message = str(exc_info.value)
        assert "fact.entity_id" in message
        assert "alpha=record_index" in message
        assert "beta=record_id" in message

    def test_no_election_and_no_override_defaults_record_id(self) -> None:
        sidecar = _entity_sidecar()
        election = resolve_election(sidecar, None)
        dim_populations = DimSourcePopulations(
            kind="widget", populations=(None,), proper_subset=False
        )
        resolved = resolve_fk_surface(election, dim_populations, None, "fact.widget_id")
        assert resolved == "record_id"


# ---------------------------------------------------------------------------
# dim_population_sub_types / dim_key_projects_surface
# ---------------------------------------------------------------------------


class TestDimPopulationSubTypes:
    def test_flat_whole_table_population_is_empty_tuple(self) -> None:
        dim_populations = DimSourcePopulations(
            kind="widget", populations=(None,), proper_subset=False
        )
        assert dim_population_sub_types(dim_populations) == ()

    def test_sub_typed_population_set_passes_through(self) -> None:
        dim_populations = DimSourcePopulations(
            kind="entity", populations=("alpha", "beta"), proper_subset=False
        )
        assert dim_population_sub_types(dim_populations) == ("alpha", "beta")


class TestDimKeyProjectsSurface:
    def test_true_when_a_declared_key_column_sources_the_surface(self) -> None:
        table_decl = TableDecl(
            name="dim_entity",
            role="dim",
            key=["code"],
            source={"grain": "records", "kind": "entity"},  # type: ignore[arg-type]
            columns=[ColumnDecl(name="code", **{"from": "presentation_id"})],
        )
        assert dim_key_projects_surface(table_decl, "presentation_id") is True
        assert dim_key_projects_surface(table_decl, "record_index") is False

    def test_false_when_no_key_column_sources_the_surface(self) -> None:
        table_decl = TableDecl(
            name="dim_entity",
            role="dim",
            key=["record_id"],
            source={"grain": "records", "kind": "entity"},  # type: ignore[arg-type]
            columns=[ColumnDecl(name="record_id", **{"from": "record_id"})],
        )
        assert dim_key_projects_surface(table_decl, "presentation_id") is False
