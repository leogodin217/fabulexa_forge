"""Tests for exporters.election: resolution, static gates, spine, guard.

Gate tests build sidecars directly via `Sidecar.from_raw` (plan-time gates are
sidecar-only, no DuckDB needed); the render-time guard
(`check_elected_key_unique`) needs a real open `Emit`, so its tests build a
minimal on-disk emit and run arbitrary VALUES-based relation SQL through it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import identity_column, write_emit

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionKindUnknown,
    ElectionMixedIdentity,
    ElectionPresentationUndeclared,
    ElectionSubTypeUnknown,
    ElectionUnionUnsafe,
    ExportError,
)
from fabulexa_forge.exporters.election import (
    build_population_spine_sql,
    check_edge_union_safety,
    check_elected_key_unique,
    check_identity_election,
    resolve_election,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import PresentationKeysInvalidError
from fabulexa_forge.reader.sidecar import KeySpace, Sidecar, union_safe

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(name: str, type_: str = "VARCHAR") -> dict[str, object]:
    return {"name": name, "type": type_}


def _records_table(
    kind: str, discriminator: bool = False, presentation_id: bool = True
) -> dict[str, object]:
    """Build a raw records__<kind> table entry.

    `discriminator=True` adds a `prop__<kind>_type` column, mirroring a
    sub-typed kind's declared discriminator (the domain itself lives in
    `enum_domains`, consulted independently by `subtype_values`).
    """
    cols = [_col("fork_path"), _col("record_id")]
    if presentation_id:
        cols.append(_col("presentation_id"))
    cols += [
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


def _raw_counter_key(prefix: str, width: int = 3) -> dict[str, object]:
    """A conformant counter-class raw partition_key (emit/false/false)."""
    return {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": {"class": "counter", "prefix": prefix, "width": width},
    }


_ENTITY_DOMAIN: dict[str, object] = {
    "entity": {"entity_type": ["alpha", "beta", "gamma"]}
}

# entity.alpha / entity.beta declared with distinct, prefix-incomparable
# counter spaces (ALPHA_ / BETA_) — a union-safe pair; gamma undeclared.
_SAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": _raw_counter_key("ALPHA_"),
            "beta": _raw_counter_key("BETA_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

# entity.alpha / entity.beta declared as bare (empty-prefix) counters — the
# ride-sharing shape: comparable prefixes, a union-unsafe pair.
_UNSAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": _raw_counter_key(""),
            "beta": _raw_counter_key(""),
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}

# Every domain sub-type declared, pairwise union-safe.
_FULLY_DECLARED_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": _raw_counter_key("ALPHA_"),
            "beta": _raw_counter_key("BETA_"),
            "gamma": _raw_counter_key("GAMMA_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _entity_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """entity: sub-typed kind (alpha/beta/gamma domain); booking: flat kind.

    `booking` never carries a presentation_id column here — no fixture
    elects presentation_id on it, and the registry's kind-membership clause
    would otherwise demand a `booking` entry in every presentation_keys
    block below, none of which declare one (they cover `entity` only).
    """
    return _sidecar(
        [
            _records_table("entity", discriminator=True),
            _records_table("booking", presentation_id=False),
        ],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys=presentation_keys,
    )


# ---------------------------------------------------------------------------
# resolve_election
# ---------------------------------------------------------------------------


class TestResolveElectionDefault:
    def test_no_keys_block_is_total_all_record_id(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        assert election.is_default("entity")
        assert election.is_default("booking")
        assert election.surface_for("booking", None) == "record_id"
        for sub_type in ("alpha", "beta", "gamma"):
            assert election.surface_for("entity", sub_type) == "record_id"


class TestResolveElectionScalarAndShorthand:
    def test_scalar_on_flat_kind(self) -> None:
        election = resolve_election(_entity_sidecar(), {"booking": "record_index"})
        assert election.surface_for("booking", None) == "record_index"

    def test_scalar_shorthand_on_sub_typed_kind_resolves_every_sub_type(self) -> None:
        election = resolve_election(
            _entity_sidecar(_FULLY_DECLARED_PRESENTATION_KEYS),
            {"entity": "presentation_id"},
        )
        for sub_type in ("alpha", "beta", "gamma"):
            assert election.surface_for("entity", sub_type) == "presentation_id"

    def test_partial_map_leaves_unlisted_sub_types_at_record_id(self) -> None:
        election = resolve_election(
            _entity_sidecar(_SAFE_PRESENTATION_KEYS),
            {"entity": {"alpha": "presentation_id"}},
        )
        assert election.surface_for("entity", "alpha") == "presentation_id"
        assert election.surface_for("entity", "beta") == "record_id"
        assert election.surface_for("entity", "gamma") == "record_id"

    def test_populations_for_preserves_declaration_order(self) -> None:
        election = resolve_election(
            _entity_sidecar(_SAFE_PRESENTATION_KEYS),
            {"entity": {"beta": "presentation_id", "alpha": "presentation_id"}},
        )
        populations = election.populations_for("entity")
        assert [p.sub_type for p in populations] == ["alpha", "beta", "gamma"]


class TestResolutionGates:
    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ElectionKindUnknown):
            resolve_election(_entity_sidecar(), {"ghost": "record_id"})

    def test_map_key_outside_domain_raises(self) -> None:
        with pytest.raises(ElectionSubTypeUnknown):
            resolve_election(_entity_sidecar(), {"entity": {"delta": "record_index"}})

    def test_map_on_flat_kind_raises(self) -> None:
        with pytest.raises(ElectionSubTypeUnknown):
            resolve_election(_entity_sidecar(), {"booking": {"x": "record_id"}})

    def test_presentation_id_without_block_names_absence(self) -> None:
        with pytest.raises(
            ElectionPresentationUndeclared, match="no presentation_keys claims"
        ):
            resolve_election(_entity_sidecar(), {"booking": "presentation_id"})

    def test_presentation_id_uncovered_population_names_entry(self) -> None:
        with pytest.raises(
            ElectionPresentationUndeclared, match="no presentation_keys registry entry"
        ):
            resolve_election(
                _entity_sidecar(_SAFE_PRESENTATION_KEYS),
                {"entity": {"gamma": "presentation_id"}},
            )

    def test_uniform_shorthand_requires_every_domain_sub_type_declared(self) -> None:
        # gamma is undeclared under _SAFE_PRESENTATION_KEYS.
        with pytest.raises(ElectionPresentationUndeclared):
            resolve_election(
                _entity_sidecar(_SAFE_PRESENTATION_KEYS), {"entity": "presentation_id"}
            )

    def test_incoherent_registry_silent_when_unused(self) -> None:
        """An incoherent block never surfaces when no population elects
        presentation_id."""
        incoherent: dict[str, object] = {
            "booking": {
                "key": {
                    "unique_within": "branch",
                    "branch_stable": True,
                    "slice_stable": True,
                    "key_space": {"class": "counter", "prefix": "X_", "width": 3},
                }
            }
        }
        sidecar = _sidecar([_records_table("booking")], presentation_keys=incoherent)
        election = resolve_election(sidecar, None)
        assert election.is_default("booking")
        election = resolve_election(sidecar, {"booking": "record_index"})
        assert election.surface_for("booking", None) == "record_index"

    def test_incoherent_registry_propagates_when_used(self) -> None:
        incoherent: dict[str, object] = {
            "booking": {
                "key": {
                    "unique_within": "branch",
                    "branch_stable": True,
                    "slice_stable": True,
                    "key_space": {"class": "counter", "prefix": "X_", "width": 3},
                }
            }
        }
        sidecar = _sidecar([_records_table("booking")], presentation_keys=incoherent)
        with pytest.raises(PresentationKeysInvalidError):
            resolve_election(sidecar, {"booking": "presentation_id"})


class TestSynthesizedKeySpaces:
    def test_record_id_class(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        pop = election.populations_for("booking")[0]
        assert pop.key_space == KeySpace(
            space_class="record_id", prefix=None, width=None
        )

    def test_record_index_class_empty_prefix_zero_width(self) -> None:
        election = resolve_election(_entity_sidecar(), {"booking": "record_index"})
        pop = election.populations_for("booking")[0]
        assert pop.key_space == KeySpace(space_class="record_index", prefix="", width=0)

    def test_record_id_unsafe_beside_record_index(self) -> None:
        record_id_space = KeySpace(space_class="record_id", prefix=None, width=None)
        record_index_space = KeySpace(space_class="record_index", prefix="", width=0)
        assert union_safe(record_id_space, record_index_space) is False

    def test_record_id_unsafe_beside_uuid(self) -> None:
        record_id_space = KeySpace(space_class="record_id", prefix=None, width=None)
        uuid_space = KeySpace(space_class="uuid", prefix=None, width=None)
        assert union_safe(record_id_space, uuid_space) is False

    def test_empty_prefix_incomparable_with_alpha_prefix(self) -> None:
        record_index_space = KeySpace(space_class="record_index", prefix="", width=0)
        alpha_space = KeySpace(space_class="counter", prefix="ALPHA_", width=3)
        assert union_safe(record_index_space, alpha_space) is True


# ---------------------------------------------------------------------------
# check_identity_election
# ---------------------------------------------------------------------------


class TestCheckIdentityElection:
    def test_same_surface_passes(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        check_identity_election(election, "entity", ["alpha", "beta"], "t_entity")

    def test_mixed_surfaces_raises(self) -> None:
        election = resolve_election(
            _entity_sidecar(_SAFE_PRESENTATION_KEYS),
            {"entity": {"alpha": "presentation_id"}},
        )
        with pytest.raises(ElectionMixedIdentity):
            check_identity_election(election, "entity", ["alpha", "beta"], "t_entity")

    def test_uniform_presentation_id_over_bare_counter_siblings_raises(self) -> None:
        election = resolve_election(
            _entity_sidecar(_UNSAFE_PRESENTATION_KEYS),
            {"entity": {"alpha": "presentation_id", "beta": "presentation_id"}},
        )
        with pytest.raises(ElectionUnionUnsafe):
            check_identity_election(election, "entity", ["alpha", "beta"], "t_entity")

    def test_single_population_call_passes_trivially(self) -> None:
        election = resolve_election(
            _entity_sidecar(_SAFE_PRESENTATION_KEYS),
            {"entity": {"alpha": "presentation_id"}},
        )
        check_identity_election(election, "entity", ["alpha"], "t_entity")


# ---------------------------------------------------------------------------
# check_edge_union_safety
# ---------------------------------------------------------------------------


class TestCheckEdgeUnionSafety:
    def test_partial_map_default_beside_digit_rendered_raises(self) -> None:
        election = resolve_election(
            _entity_sidecar(), {"entity": {"alpha": "record_index"}}
        )
        with pytest.raises(ElectionUnionUnsafe):
            check_edge_union_safety(
                election, "entity", ["alpha", "beta"], "orders.entity_id"
            )

    def test_record_index_beside_prefixed_space_passes(self) -> None:
        election = resolve_election(
            _entity_sidecar(_SAFE_PRESENTATION_KEYS),
            {"entity": {"alpha": "record_index", "beta": "presentation_id"}},
        )
        check_edge_union_safety(
            election, "entity", ["alpha", "beta"], "orders.entity_id"
        )

    def test_surface_override_presentation_id_uncovered_population_raises(self) -> None:
        election = resolve_election(_entity_sidecar(_SAFE_PRESENTATION_KEYS), None)
        with pytest.raises(ElectionPresentationUndeclared):
            check_edge_union_safety(
                election,
                "entity",
                ["alpha", "gamma"],
                "fact.entity_id",
                surface_override="presentation_id",
            )

    def test_absent_target_kind_raises_key_error(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        with pytest.raises(KeyError):
            check_edge_union_safety(election, "ghost", ["x"], "fact.ghost_id")


# ---------------------------------------------------------------------------
# Election.surface_for / populations_for raise KeyError
# ---------------------------------------------------------------------------


class TestElectionKeyErrors:
    def test_surface_for_unknown_kind_raises(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        with pytest.raises(KeyError):
            election.surface_for("ghost", None)

    def test_surface_for_unknown_sub_type_raises(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        with pytest.raises(KeyError):
            election.surface_for("entity", "delta")

    def test_populations_for_unknown_kind_raises(self) -> None:
        election = resolve_election(_entity_sidecar(), None)
        with pytest.raises(KeyError):
            election.populations_for("ghost")


# ---------------------------------------------------------------------------
# build_population_spine_sql
# ---------------------------------------------------------------------------


class TestBuildPopulationSpineSql:
    def test_composes_records_relation_with_in_list(self) -> None:
        sidecar = _entity_sidecar()
        sql = build_population_spine_sql(sidecar, "trunk", "entity", ["alpha", "beta"])
        assert 'SELECT "record_id" FROM (' in sql
        assert "records__entity" in sql
        assert 'WHERE "_spine"."prop__entity_type" IN (' in sql
        assert "'alpha', 'beta'" in sql

    def test_quote_doubling(self) -> None:
        sidecar = _sidecar(
            [_records_table("widget", discriminator=True)],
            enum_domains={"widget": {"widget_type": ["a'b", "c"]}},
        )
        sql = build_population_spine_sql(sidecar, "trunk", "widget", ["a'b"])
        assert "'a''b'" in sql

    def test_order_preserved(self) -> None:
        sidecar = _entity_sidecar()
        sql = build_population_spine_sql(sidecar, "trunk", "entity", ["beta", "alpha"])
        assert sql.index("'beta'") < sql.index("'alpha'")

    def test_refuses_empty_set(self) -> None:
        with pytest.raises(ExportError):
            build_population_spine_sql(_entity_sidecar(), "trunk", "entity", [])

    def test_refuses_full_domain(self) -> None:
        with pytest.raises(ExportError):
            build_population_spine_sql(
                _entity_sidecar(), "trunk", "entity", ["alpha", "beta", "gamma"]
            )

    def test_refuses_out_of_domain_value(self) -> None:
        with pytest.raises(ExportError):
            build_population_spine_sql(_entity_sidecar(), "trunk", "entity", ["delta"])

    def test_refuses_non_sub_typed_kind(self) -> None:
        with pytest.raises(ExportError):
            build_population_spine_sql(_entity_sidecar(), "trunk", "booking", ["x"])


# ---------------------------------------------------------------------------
# check_elected_key_unique
# ---------------------------------------------------------------------------


def _minimal_emit(tmp_path: Path) -> Path:
    """An emit with no meaningful records data — check_elected_key_unique
    reads no sidecar table, only `emit.query` over caller-supplied SQL."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__dummy",
                "category": "records",
                "record_kind": "dummy",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "created_sim_time", "type": "BIGINT"},
                    {"name": "active", "type": "BOOLEAN"},
                    {"name": "deactivated_at", "type": "BIGINT"},
                    {"name": "last_mutation_sim_time", "type": "BIGINT"},
                    identity_column("record_index", "BIGINT"),
                ],
                "rows": 0,
            }
        ],
    )
    return tmp_path


class TestCheckElectedKeyUnique:
    def test_passes_on_conformant_relation(self, tmp_path: Path) -> None:
        with open_emit(_minimal_emit(tmp_path)) as emit:
            relation_sql = (
                "SELECT * FROM (VALUES ('r1','ALPHA_1'),('r2','ALPHA_2')) "
                "AS t(record_id, presentation_id)"
            )
            check_elected_key_unique(
                emit, relation_sql, "presentation_id", None, "orders.id"
            )

    def test_fails_on_null_inside_consumed_set(self, tmp_path: Path) -> None:
        with open_emit(_minimal_emit(tmp_path)) as emit:
            relation_sql = (
                "SELECT * FROM (VALUES ('r1', CAST(NULL AS VARCHAR)),"
                "('r2','ALPHA_2')) AS t(record_id, presentation_id)"
            )
            with pytest.raises(ElectedKeyDuplicate):
                check_elected_key_unique(
                    emit, relation_sql, "presentation_id", None, "orders.id"
                )

    def test_fails_duplicated_row_mutated_value(self, tmp_path: Path) -> None:
        with open_emit(_minimal_emit(tmp_path)) as emit:
            relation_sql = (
                "SELECT * FROM (VALUES ('r1','ALPHA_1'),('r1','ALPHA_999')) "
                "AS t(record_id, presentation_id)"
            )
            with pytest.raises(ElectedKeyDuplicate) as excinfo:
                check_elected_key_unique(
                    emit, relation_sql, "presentation_id", None, "orders.id"
                )
            message = str(excinfo.value)
            assert "orders.id" in message
            assert "presentation_id" in message
            assert "rows=2" in message
            assert "distinct record_id=1" in message
            assert "distinct presentation_id=2" in message
            assert "NULL presentation_id=0" in message

    def test_spine_restriction_excludes_out_of_set_violation(
        self, tmp_path: Path
    ) -> None:
        with open_emit(_minimal_emit(tmp_path)) as emit:
            relation_sql = (
                "SELECT * FROM (VALUES ('r1','ALPHA_1'),('r2','ALPHA_2'),"
                "('bad', CAST(NULL AS VARCHAR))) AS t(record_id, presentation_id)"
            )
            spine_sql = "SELECT * FROM (VALUES ('r1'),('r2')) AS s(record_id)"
            check_elected_key_unique(
                emit, relation_sql, "presentation_id", spine_sql, "orders.id"
            )
