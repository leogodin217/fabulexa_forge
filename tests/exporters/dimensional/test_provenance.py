"""Tests for dimensional provenance stamping.

Covers `ColumnProvenance` / `KindValueEntry` (`exporters/query_spec.py`) as
stamped by `build_grain_sql`'s fifth element and forwarded onto `QuerySpec`
by `dimensional/engine.py`'s `build_query_specs`: the resolution table from
the documentation-channel sprint spec § Phase 3 — pass-through, renamed, and
lookup columns carry a provenance entry keyed by output column name; a
derived: timestamp column keeps its entry too; computed columns (derived:
ordinal / elapsed, SCD-2 valid_from / valid_to) carry none; dimensional's
`kind_values` stays empty on every spec; stamping is deterministic across
repeated compiles of the same plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    LookupClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.query_spec import ColumnProvenance
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.query_spec import QuerySpec
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Fixture emit: team / actor (+ a status change) / tick_decision
# ---------------------------------------------------------------------------

_TEAM_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__team_name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__full_name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__team_id",
        "VARCHAR",
        references="team",
        history_tracked=False,
        temporal_class="constant",
    ),
    identity_column("ref_index__team_id", "BIGINT"),
]

_DECISION_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__journey_id", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__decision_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_provenance_emit(tmp_path: Path) -> Path:
    """Build a fixture emit: one team, one actor with a tracked status
    change, and two tick_decisions in the same journey.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__team", _TEAM_COLUMNS))
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, None, 0, 0, "Cardiology"],
    )

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "a1",
            10,
            True,
            None,
            25,
            0,
            "Dr. Smith",
            "under_treatment",
            "t1",
            0,
        ],
    )

    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a1", "status", 15, "admitted"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a1", "status", 25, "under_treatment"],
    )

    conn.execute(_create_ddl("records__tick_decision", _DECISION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__tick_decision" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d1", 100, True, None, 100, 0, "j1", "arrival"],
    )
    conn.execute(
        'INSERT INTO "records__tick_decision" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "d2", 145, True, None, 145, 1, "j1", "triage"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__team", "records", _TEAM_COLUMNS, 1, record_kind="team"
            ),
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 1, record_kind="actor"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
            _table_spec(
                "records__tick_decision",
                "records",
                _DECISION_COLUMNS,
                2,
                record_kind="tick_decision",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Table declarations
# ---------------------------------------------------------------------------


def _dim_actor_table_decl() -> TableDecl:
    """dim_actor (type1): from, correlation (rename), derived: timestamp,
    and lookup column modes -- every column faithfully carried."""
    return TableDecl(
        name="dim_actor",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="actor"),
        key=["actor_id"],
        columns=[
            ColumnDecl(name="actor_id", **{"from": "record_id"}),
            ColumnDecl(name="display_name", correlation="prop__full_name"),
            ColumnDecl(
                name="joined_at",
                derived=DerivedSpec(timestamp=TimestampSpec(source="created_sim_time")),
            ),
            ColumnDecl(
                name="team_name",
                lookup=LookupClause(property="team_name", to="team"),
            ),
        ],
    )


def _dim_actor_scd_table_decl() -> TableDecl:
    """dim_actor_scd (type2): carried id/status plus computed SCD-2
    valid_from/valid_to bounds."""
    return TableDecl(
        name="dim_actor_scd",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )


def _fact_decision_table_decl() -> TableDecl:
    """fact_decision: from columns plus computed ordinal + elapsed columns."""
    return TableDecl(
        name="fact_decision",
        role="fact",
        source=SourceDecl(grain="records", kind="tick_decision"),
        key=["decision_id"],
        columns=[
            ColumnDecl(name="decision_id", **{"from": "record_id"}),
            ColumnDecl(name="journey_id", **{"from": "prop__journey_id"}),
            ColumnDecl(name="changed_at", **{"from": "last_mutation_sim_time"}),
            ColumnDecl(
                name="seq",
                derived=DerivedSpec(
                    ordinal=OrdinalSpec(
                        partition_by="journey_id", order_by="changed_at"
                    )
                ),
            ),
            ColumnDecl(
                name="wait_minutes",
                derived=DerivedSpec(
                    elapsed=ElapsedSpec(
                        correlate_on="prop__journey_id",
                        other_where={"prop__decision_type": "arrival"},
                        start_source="last_mutation_sim_time",
                        end_source="last_mutation_sim_time",
                        unit="minutes",
                    )
                ),
            ),
        ],
    )


def _build_config(*table_decls: TableDecl) -> DimensionalConfig:
    """Build a DimensionalConfig from one or more table declarations."""
    return DimensionalConfig(tables=list(table_decls))


def _compile_specs(emit: "Emit", config: DimensionalConfig) -> "dict[str, QuerySpec]":
    """Compile a full-export (window=None) plan, keyed by table_name."""
    return {
        spec.table_name: spec
        for spec in build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
    }


# ---------------------------------------------------------------------------
# Resolution table: carried vs. computed columns
# ---------------------------------------------------------------------------


def test_dim_pass_through_column_carries_source(tmp_path: Path) -> None:
    """A `from` column stamps (records__<kind>, <source column>)."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_dim_actor_table_decl()))

    assert specs["dim_actor"].provenance["actor_id"] == ColumnProvenance(
        source_table="records__actor", source_column="record_id"
    )


def test_renamed_column_keyed_by_output_name(tmp_path: Path) -> None:
    """A `correlation` (rename) column's entry keys on the output name."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_dim_actor_table_decl()))

    assert specs["dim_actor"].provenance["display_name"] == ColumnProvenance(
        source_table="records__actor", source_column="prop__full_name"
    )


def test_lookup_column_carries_looked_up_source(tmp_path: Path) -> None:
    """A `lookup` column's entry names the looked-up property's own (table,
    column) -- not the grain's source table."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_dim_actor_table_decl()))

    assert specs["dim_actor"].provenance["team_name"] == ColumnProvenance(
        source_table="records__team", source_column="prop__team_name"
    )


def test_temporal_rendered_column_keeps_provenance_entry(tmp_path: Path) -> None:
    """A `derived: timestamp` column still stamps its source column."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_dim_actor_table_decl()))

    assert specs["dim_actor"].provenance["joined_at"] == ColumnProvenance(
        source_table="records__actor", source_column="created_sim_time"
    )


def test_dim_key_column_stamped_like_any_carried_column(tmp_path: Path) -> None:
    """A dim's declared `key` column is an ordinary carried column -- the
    key list narrows nothing about which columns get a provenance entry."""
    emit_dir = _build_provenance_emit(tmp_path)
    table_decl = _dim_actor_table_decl()
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(table_decl))

    assert set(table_decl.key) <= specs["dim_actor"].provenance.keys()


def test_scd2_valid_from_valid_to_have_no_entry(tmp_path: Path) -> None:
    """SCD-2 valid_from/valid_to are computed -- no provenance entry -- while
    the carried id/status columns still stamp."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_dim_actor_scd_table_decl()))

    provenance = specs["dim_actor_scd"].provenance
    assert "valid_from" not in provenance
    assert "valid_to" not in provenance
    assert provenance["id"] == ColumnProvenance(
        source_table="records__actor", source_column="record_id"
    )
    assert provenance["status"] == ColumnProvenance(
        source_table="records__actor", source_column="prop__status"
    )


def test_fact_grain_columns_carried_computed_columns_absent(tmp_path: Path) -> None:
    """A fact's `from` columns stamp; `derived: ordinal` (seq) and
    `derived: elapsed` (wait_minutes) get no entry."""
    emit_dir = _build_provenance_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, _build_config(_fact_decision_table_decl()))

    provenance = specs["fact_decision"].provenance
    assert provenance["decision_id"] == ColumnProvenance(
        source_table="records__tick_decision", source_column="record_id"
    )
    assert provenance["journey_id"] == ColumnProvenance(
        source_table="records__tick_decision", source_column="prop__journey_id"
    )
    assert provenance["changed_at"] == ColumnProvenance(
        source_table="records__tick_decision", source_column="last_mutation_sim_time"
    )
    assert "seq" not in provenance
    assert "wait_minutes" not in provenance


# ---------------------------------------------------------------------------
# kind_values / determinism
# ---------------------------------------------------------------------------


def test_kind_values_empty_on_every_dimensional_spec(tmp_path: Path) -> None:
    """Dimensional has no kind-name-as-value output column: kind_values
    stays empty on every compiled spec (a pinned fact, not a default left to
    chance)."""
    emit_dir = _build_provenance_emit(tmp_path)
    config = _build_config(_dim_actor_table_decl(), _fact_decision_table_decl())
    with open_emit(emit_dir) as emit:
        specs = _compile_specs(emit, config)

    for spec in specs.values():
        assert spec.kind_values == {}


def test_provenance_deterministic_across_compiles(tmp_path: Path) -> None:
    """Two compiles of the same plan against the same emit yield equal
    provenance maps."""
    emit_dir = _build_provenance_emit(tmp_path)
    config = _build_config(_dim_actor_table_decl(), _fact_decision_table_decl())

    with open_emit(emit_dir) as emit:
        first = _compile_specs(emit, config)
        second = _compile_specs(emit, config)

    assert {name: spec.provenance for name, spec in first.items()} == {
        name: spec.provenance for name, spec in second.items()
    }
