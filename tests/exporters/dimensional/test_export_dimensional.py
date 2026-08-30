"""Tests for export_dimensional: the dispatcher that wires build_query_specs to writers.

Verifies: CSV dispatch (one file per table), DuckDB dispatch (one file),
row counts for every table (including 0-row tables), idempotent re-run
(identical row counts and CSV bytes), ExportRuntimeError propagation.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    FkClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.dimensional.engine import (
    build_query_specs,
    export_dimensional,
)
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit fixture for export_dimensional tests
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_JOURNEY_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__actor_id",
        "type": "VARCHAR",
        "references": "actor",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    identity_column("ref_index__actor_id", "BIGINT"),
]

_HISTORY_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_export_emit(tmp_path: Path) -> Path:
    """Build a test emit with actor (SCD-2 capable) + journey (FK source) + history."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__journey_instance", _JOURNEY_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    # Two actors
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, None, 100, 0, "Alice", "active"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", 0, True, None, 200, 1, "Bob", "active"],
    )

    # Two journeys referencing actors (ref_index__actor_id mirrors the
    # referenced actor's record_index)
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j001", 0, True, 10, 0, "a001", 0],
    )
    conn.execute(
        'INSERT INTO "records__journey_instance" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "j002", 0, True, 20, 1, "a002", 1],
    )

    # History: status changes for a001 at two sim_times
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 10, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 50, "active"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ACTOR_COLUMNS,
                2,
                record_kind="actor",
            ),
            _table_spec(
                "records__journey_instance",
                "records",
                _JOURNEY_COLUMNS,
                2,
                record_kind="journey_instance",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        extra={
            "enum_domains": {
                "journey_instance": {"entity_type": enum_options("type_a", "type_b")},
            }
        },
    )
    return tmp_path


def _make_export_config() -> ExportConfig:
    """Build a multi-table ExportConfig covering SCD-2, Type-1, FK fact, empty table."""
    dim_actor_scd2 = TableDecl(
        name="dim_actor",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
        ],
    )

    dim_journey = TableDecl(
        name="dim_journey",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="journey_instance"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="actor_id", **{"from": "prop__actor_id"}),
        ],
    )

    fact_journey_with_fk = TableDecl(
        name="fact_journey_event",
        role="fact",
        source=SourceDecl(grain="records", kind="journey_instance"),
        key=["journey_id"],
        columns=[
            ColumnDecl(name="journey_id", **{"from": "record_id"}),
            ColumnDecl(name="actor_raw_id", **{"from": "prop__actor_id"}),
            ColumnDecl(
                name="actor_fk",
                fk=FkClause(to="dim_actor", via="reference"),
            ),
            ColumnDecl(
                name="journey_seq",
                derived=DerivedSpec(
                    ordinal=OrdinalSpec(
                        partition_by="journey_id",
                        order_by="actor_raw_id",
                    )
                ),
            ),
        ],
    )

    # This table uses a nonexistent property value filter → zero rows
    fact_empty = TableDecl(
        name="fact_empty",
        role="fact",
        source=SourceDecl(
            grain="history_point",
            kind="actor",
            property="nonexistent_property",
        ),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="val", **{"from": "value"}),
        ],
    )

    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[dim_actor_scd2, dim_journey, fact_journey_with_fk, fact_empty]
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_dimensional_csv_writes_one_file_per_table(tmp_path: Path) -> None:
    """export_dimensional(fmt='csv') writes one .csv per declared table."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            None,
        )

    table_names = {table.name for table in report.tables}
    assert table_names == {
        "dim_actor",
        "dim_journey",
        "fact_journey_event",
        "fact_empty",
    }
    for table_name in table_names:
        assert (out_dir / f"{table_name}.csv").exists()


def test_export_dimensional_csv_empty_table_is_header_only(tmp_path: Path) -> None:
    """An empty-grain table produces a header-only CSV, not a missing file."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            None,
        )

    row_counts = {table.name: table.row_count for table in report.tables}
    assert row_counts["fact_empty"] == 0
    csv_path = out_dir / "fact_empty.csv"
    assert csv_path.exists()
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1  # header only
    assert rows[0] != []  # header has column names


def test_export_dimensional_duckdb_writes_all_tables(tmp_path: Path) -> None:
    """export_dimensional(fmt='duckdb') writes every declared table to one file."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_path = tmp_path / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            None,
            discard_notice_sink,
            None,
        )

    table_names = {table.name for table in report.tables}
    assert table_names == {
        "dim_actor",
        "dim_journey",
        "fact_journey_event",
        "fact_empty",
    }
    assert out_path.exists()
    out_conn = duckdb.connect(str(out_path), read_only=True)
    tables = {row[0] for row in out_conn.execute("SHOW TABLES").fetchall()}
    out_conn.close()
    assert {"dim_actor", "dim_journey", "fact_journey_event", "fact_empty"}.issubset(
        tables
    )


def test_export_dimensional_duckdb_empty_table_typed_not_dropped(
    tmp_path: Path,
) -> None:
    """An empty-grain DuckDB table is an empty typed table, not dropped."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    out_path = tmp_path / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            None,
            discard_notice_sink,
            None,
        )

    row_counts = {table.name: table.row_count for table in report.tables}
    assert row_counts["fact_empty"] == 0
    out_conn = duckdb.connect(str(out_path), read_only=True)
    schema = out_conn.execute("DESCRIBE fact_empty").fetchall()
    out_conn.close()
    assert len(schema) > 0


def test_export_dimensional_idempotent_csv(tmp_path: Path) -> None:
    """Exporting twice to CSV yields identical row counts and identical bytes."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    config = _make_export_config()

    out_dir1 = tmp_path / "run1"
    out_dir1.mkdir()
    out_dir2 = tmp_path / "run2"
    out_dir2.mkdir()

    with open_emit(emit_dir) as emit:
        report1 = export_dimensional(
            emit,
            config,
            out_dir1,
            "csv",
            None,
            discard_notice_sink,
            None,
        )
    with open_emit(emit_dir) as emit:
        report2 = export_dimensional(
            emit,
            config,
            out_dir2,
            "csv",
            None,
            discard_notice_sink,
            None,
        )

    counts1 = {table.name: table.row_count for table in report1.tables}
    counts2 = {table.name: table.row_count for table in report2.tables}
    assert counts1 == counts2
    for table_name in counts1:
        f1 = (out_dir1 / f"{table_name}.csv").read_bytes()
        f2 = (out_dir2 / f"{table_name}.csv").read_bytes()
        assert f1 == f2, f"CSV bytes differ for {table_name}"


def test_export_dimensional_idempotent_duckdb_row_counts(tmp_path: Path) -> None:
    """Exporting twice to DuckDB yields identical row counts."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    config = _make_export_config()

    out1 = tmp_path / "out1.duckdb"
    out2 = tmp_path / "out2.duckdb"

    with open_emit(emit_dir) as emit:
        report1 = export_dimensional(
            emit,
            config,
            out1,
            "duckdb",
            None,
            discard_notice_sink,
            None,
        )
    with open_emit(emit_dir) as emit:
        report2 = export_dimensional(
            emit,
            config,
            out2,
            "duckdb",
            None,
            discard_notice_sink,
            None,
        )

    counts1 = {table.name: table.row_count for table in report1.tables}
    counts2 = {table.name: table.row_count for table in report2.tables}
    assert counts1 == counts2


def test_export_dimensional_writer_failure_raises_export_runtime_error(
    tmp_path: Path,
) -> None:
    """A writer failure surfaces as ExportRuntimeError."""
    emit_dir = _build_export_emit(tmp_path / "emit")
    bad_path = tmp_path / "no_parent" / "sub" / "out.duckdb"
    config = _make_export_config()

    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportRuntimeError):
            export_dimensional(
                emit,
                config,
                bad_path,
                "duckdb",
                None,
                discard_notice_sink,
                None,
            )


# ---------------------------------------------------------------------------
# Records-grain fact carrying its own structural instants
# ---------------------------------------------------------------------------


def _build_records_instant_emit(tmp_path: Path) -> Path:
    """Build an emit with one records kind: one deactivated, one still-active row.

    Mirrors the fact-from-records recipe: a single `entity` kind whose two
    records exercise both a NULL and a populated `deactivated_at`.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ACTOR_COLUMNS))

    # Deactivated record: created at 0, deactivated (and last touched) at 50s.
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "e001",
            0,
            False,
            50_000_000_000,
            50_000_000_000,
            0,
            "Gizmo",
            "retired",
        ],
    )
    # Still-active record: created at 0, never deactivated, last touched at 30s.
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "e002", 0, True, None, 30_000_000_000, 1, "Widget", "active"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity",
                "records",
                _ACTOR_COLUMNS,
                2,
                record_kind="entity",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100_000_000_000}],
        extra={
            "enum_domains": {"entity": {}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
        schema_valid=False,
    )
    return tmp_path


def _make_records_instant_config() -> ExportConfig:
    """Return a config for a records-grain fact carrying its three instants."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="fact_entity",
                    role="fact",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(
                            name="created_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="created_sim_time")
                            ),
                        ),
                        ColumnDecl(
                            name="closed_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="deactivated_at")
                            ),
                        ),
                        ColumnDecl(
                            name="last_touched_at",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="last_mutation_sim_time")
                            ),
                        ),
                    ],
                )
            ]
        ),
    )


def test_records_grain_instant_columns_validate_and_export(tmp_path: Path) -> None:
    """created_sim_time and deactivated_at render wallclock through the anchor.

    This exact config (created_sim_time / deactivated_at as `derived:
    timestamp` sources on a records grain) errored before the
    structural-temporal sprint's allowlist widened to three instants.
    """
    emit_dir = _build_records_instant_emit(tmp_path / "emit")
    config = _make_records_instant_config()
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            discard_notice_sink,
            None,
        )

    conn = duckdb.connect(str(out_path), read_only=True)
    rows = conn.execute(
        'SELECT "id", "created_at", "closed_at", "last_touched_at"'
        ' FROM "fact_entity" ORDER BY "id"'
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    by_id = {row[0]: row for row in rows}

    deactivated = by_id["e001"]
    assert deactivated[1] == datetime(2024, 1, 1, 0, 0, 0)
    assert deactivated[2] == datetime(2024, 1, 1, 0, 0, 50)
    assert deactivated[3] == datetime(2024, 1, 1, 0, 0, 50)

    still_active = by_id["e002"]
    assert still_active[1] == datetime(2024, 1, 1, 0, 0, 0)
    assert still_active[2] is None
    assert still_active[3] == datetime(2024, 1, 1, 0, 0, 30)


# ---------------------------------------------------------------------------
# List-valued predicates (list-valued-predicates sprint, Phase 2): the
# motivating multi-process fact table, end-to-end.
# ---------------------------------------------------------------------------

_TICK_DECISION_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__decision_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]


def _build_multi_process_emit(tmp_path: Path) -> Path:
    """Emit with five tick_decision rows spanning five discriminator values.

    Mirrors the design doc's motivating NHS scenario: several clinical
    processes distinguished by prop__decision_type, none declared in
    enum_domains (a modelling discriminator, not a sub-type tag).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__tick_decision", _TICK_DECISION_COLUMNS))

    decision_types = [
        "ed_arrival",
        "triage",
        "ed_assessment",
        "ed_diagnosis",
        "surgery_performed",
    ]
    for i, decision_type in enumerate(decision_types):
        conn.execute(
            'INSERT INTO "records__tick_decision" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", f"td{i:03d}", i, True, i, i, decision_type],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__tick_decision",
                "records",
                _TICK_DECISION_COLUMNS,
                len(decision_types),
                record_kind="tick_decision",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _make_multi_process_config() -> DimensionalConfig:
    """Group four decision types into one fact table; a scalar sibling table."""
    fact_emergency_care = TableDecl(
        name="fact_emergency_care",
        role="fact",
        source=SourceDecl(
            grain="records",
            kind="tick_decision",
            filter={
                "prop__decision_type": [
                    "ed_arrival",
                    "triage",
                    "ed_assessment",
                    "ed_diagnosis",
                ]
            },
        ),
        key=["ed_event_id"],
        columns=[
            ColumnDecl(name="ed_event_id", **{"from": "record_id"}),
            ColumnDecl(name="milestone", **{"from": "prop__decision_type"}),
        ],
    )
    fact_surgery = TableDecl(
        name="fact_surgery",
        role="fact",
        source=SourceDecl(
            grain="records",
            kind="tick_decision",
            filter={"prop__decision_type": "surgery_performed"},
        ),
        key=["event_id"],
        columns=[
            ColumnDecl(name="event_id", **{"from": "record_id"}),
            ColumnDecl(name="milestone", **{"from": "prop__decision_type"}),
        ],
    )
    return DimensionalConfig(tables=[fact_emergency_care, fact_surgery])


def test_multi_process_fact_table_from_list_filter_end_to_end(tmp_path: Path) -> None:
    """A three-plus-value list filter exports one table grouping every listed
    process; a scalar-filtered sibling table renders its byte-identical `=`
    form alongside it (the motivating case, § Purpose)."""
    emit_dir = _build_multi_process_emit(tmp_path / "emit")
    config = _make_multi_process_config()

    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        emergency_spec = next(s for s in specs if s.table_name == "fact_emergency_care")
        surgery_spec = next(s for s in specs if s.table_name == "fact_surgery")

        assert "IN (" in emergency_spec.sql
        emergency_rows = emit.query_arrow(emergency_spec.sql, ()).to_pydict()

        assert " = " in surgery_spec.sql
        assert "IN (" not in surgery_spec.sql
        surgery_rows = emit.query_arrow(surgery_spec.sql, ()).to_pydict()

    assert set(emergency_rows["milestone"]) == {
        "ed_arrival",
        "triage",
        "ed_assessment",
        "ed_diagnosis",
    }
    assert len(emergency_rows["ed_event_id"]) == 4
    assert surgery_rows["milestone"] == ["surgery_performed"]
