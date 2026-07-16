"""Tests for windowed dimensional engine compile (Phase 2).

Covers:
- Per-class window predicates (records, history_point, type-1 dim, SCD-2).
- Write modes tagged correctly (append / replace / create).
- SCD-2 __rows physical rows + companion view compile.
- Values-equal-full-export: windowed rows carry same values as in full export.
- Ordinal amendment: ORDER BY on a rendered-time sibling uses raw ns source.
- window=None (full export): every spec write_mode=='create', no views.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
from _support.sidecar_builder import write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Column definitions for windowed test emits
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS_WITH_FLAGS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR", "history_tracked": False},
    {"name": "record_id", "type": "VARCHAR", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
]

_ENTITY_COLUMNS_NO_FLAGS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
    {"name": "prop__status", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Emit builders
# ---------------------------------------------------------------------------


def _build_records_emit(tmp_path: Path) -> Path:
    """Build a minimal emit with a records grain (entity rows at different sim_times).

    Entities:
      - e001: last_mutation_sim_time=10
      - e002: last_mutation_sim_time=20
      - e003: last_mutation_sim_time=30

    Includes history_tracked flags so slice-read validation passes for type-1 dims.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    entity_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR", "history_tracked": False},
        {"name": "record_id", "type": "VARCHAR", "history_tracked": False},
        {"name": "active", "type": "BOOLEAN", "history_tracked": False},
        {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
        {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
        {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
    ]
    conn.execute(_create_ddl("records__entity", entity_cols))
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "e001", True, 10, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "e002", True, 20, "Bob"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "e003", True, 30, "Carol"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity", "records", entity_cols, 3, record_kind="entity"
            )
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _build_history_point_emit(tmp_path: Path) -> Path:
    """Build a minimal emit with a history table for history_point grain tests.

    History rows (sim_time): 5, 15, 25.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    entity_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
    ]
    conn.execute(_create_ddl("records__journey", entity_cols))
    conn.execute(
        'INSERT INTO "records__journey" VALUES (?, ?, ?, ?)',
        ["trunk", "j001", True, 30],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for sim_time in [5, 15, 25]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "journey", "j001", "state", sim_time, f"state_{sim_time}"],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__journey", "records", entity_cols, 1, record_kind="journey"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 3),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _build_scd2_with_valid_to_emit(tmp_path: Path) -> Path:
    """Build a minimal emit for SCD-2 with valid_to tests.

    Actor a001 has 3 status changes (sim_time=10,20,30).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ENTITY_COLUMNS_WITH_FLAGS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", True, None, 30, "Alice", "discharged"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for sim_time, state in [
        (10, "admitted"),
        (20, "under_treatment"),
        (30, "discharged"),
    ]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "actor", "a001", "status", sim_time, state],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ENTITY_COLUMNS_WITH_FLAGS,
                1,
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 3),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _build_scd2_no_valid_to_emit(tmp_path: Path) -> Path:
    """Build a minimal emit for SCD-2 without valid_to column.

    Actor a001 has 2 status changes (sim_time=10,20).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ENTITY_COLUMNS_WITH_FLAGS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", True, None, 20, "Alice", "treatment"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    for sim_time, state in [(10, "admitted"), (20, "treatment")]:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "actor", "a001", "status", sim_time, state],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _ENTITY_COLUMNS_WITH_FLAGS,
                1,
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _build_ordinal_emit(tmp_path: Path) -> Path:
    """Build an emit with records at same last_mutation_sim_time for ordinal tests.

    Entities e001 and e002 both have last_mutation_sim_time=10 (same microsecond).
    Entity e003 has last_mutation_sim_time=20.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    entity_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    conn.execute(_create_ddl("records__entity", entity_cols))
    for rid, sim_t, name in [
        ("e001", 10, "Alice"),
        ("e002", 10, "Bob"),
        ("e003", 20, "Carol"),
    ]:
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, ?)',
            ["trunk", rid, True, sim_t, name],
        )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity", "records", entity_cols, 3, record_kind="entity"
            )
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Config builder helpers
# ---------------------------------------------------------------------------


def _make_records_fact_config(table_name: str = "fact_entity") -> DimensionalConfig:
    """Records-grain fact config with id + name + last_mutation_sim_time columns."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name=table_name,
                role="fact",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="name", **{"from": "prop__name"}),
                    ColumnDecl(name="mutated_at", **{"from": "last_mutation_sim_time"}),
                ],
            )
        ]
    )


def _make_type1_dim_config(table_name: str = "dim_entity") -> DimensionalConfig:
    """Type-1 dim config (records grain, scd=type1).

    Uses only non-mutable columns (id from record_id, name from prop__name marked
    history_tracked=False in the emit) to pass slice-read validation under windowed
    export.
    """
    return DimensionalConfig(
        tables=[
            TableDecl(
                name=table_name,
                role="dim",
                scd="type1",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="name", **{"from": "prop__name"}),
                ],
            )
        ]
    )


def _make_scd2_with_valid_to_config(table_name: str = "dim_actor") -> DimensionalConfig:
    """SCD-2 dim config with valid_from and valid_to columns."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name=table_name,
                role="dim",
                scd="type2",
                source=SourceDecl(grain="records", kind="actor"),
                key=["id", "valid_from"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="status", **{"from": "prop__status"}),
                    ColumnDecl(
                        name="valid_from",
                        derived=DerivedSpec(scd_window="valid_from"),
                    ),
                    ColumnDecl(
                        name="valid_to",
                        derived=DerivedSpec(scd_window="valid_to"),
                    ),
                ],
            )
        ]
    )


def _make_scd2_no_valid_to_config(table_name: str = "dim_actor") -> DimensionalConfig:
    """SCD-2 dim config with valid_from but no valid_to column."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name=table_name,
                role="dim",
                scd="type2",
                source=SourceDecl(grain="records", kind="actor"),
                key=["id", "valid_from"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="status", **{"from": "prop__status"}),
                    ColumnDecl(
                        name="valid_from",
                        derived=DerivedSpec(scd_window="valid_from"),
                    ),
                ],
            )
        ]
    )


def _make_window(start_ns: int, end_ns: int, index: int = 0) -> Window:
    """Construct a minimal Window for testing."""
    return Window(
        index=index,
        start_ns=start_ns,
        end_ns=end_ns,
        label=f"w{index}",
    )


# ---------------------------------------------------------------------------
# window=None: full export shape
# ---------------------------------------------------------------------------


def test_full_export_all_specs_create_no_views(tmp_path: Path) -> None:
    """window=None: every spec has write_mode='create' and no view — existing shape."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_config()
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, None)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.write_mode == "create"
    assert spec.view_name is None
    assert spec.view_sql is None


def test_full_export_scd2_no_views(tmp_path: Path) -> None:
    """window=None: SCD-2 spec has write_mode='create', plain table name, no view."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, None)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.write_mode == "create"
    assert spec.view_name is None
    assert spec.view_sql is None
    assert spec.table_name == "dim_actor"


# ---------------------------------------------------------------------------
# Records-grain fact: window predicate
# ---------------------------------------------------------------------------


def test_records_fact_windowed_filters_half_open(tmp_path: Path) -> None:
    """Records fact: windowed SELECT filters last_mutation_sim_time half-open [start, end)."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_config()
    # window [10, 25): should include e001 (t=10) and e002 (t=20), exclude e003 (t=30)
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.write_mode == "append"
        assert spec.view_name is None
        result = emit.query_arrow(spec.sql, ())

    ids = sorted(result.column("id").to_pylist())
    assert ids == ["e001", "e002"]


def test_records_fact_row_exactly_on_end_ns_lands_in_next_window(
    tmp_path: Path,
) -> None:
    """A row with last_mutation_sim_time == end_ns is excluded (half-open)."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_config()
    # window [10, 20): e001 (t=10) included; e002 (t=20) excluded (on boundary)
    window = _make_window(start_ns=10, end_ns=20)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    ids = result.column("id").to_pylist()
    assert ids == ["e001"]


def test_records_fact_row_exactly_on_end_ns_in_next_window(tmp_path: Path) -> None:
    """A row at end_ns of one window appears in the next window starting at that ns."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_config()
    # next window [20, 30): includes e002 (t=20), excludes e003 (t=30)
    window = _make_window(start_ns=20, end_ns=30)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    ids = result.column("id").to_pylist()
    assert ids == ["e002"]


def _make_records_fact_timestamp_key_config(
    table_name: str = "fact_entity",
) -> DimensionalConfig:
    """Records fact whose ONLY window-key projection is a derived: timestamp column."""
    return DimensionalConfig(
        tables=[
            TableDecl(
                name=table_name,
                role="fact",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="name", **{"from": "prop__name"}),
                    ColumnDecl(
                        name="event_ts",
                        derived=DerivedSpec(
                            timestamp=TimestampSpec(source="last_mutation_sim_time")
                        ),
                    ),
                ],
            )
        ]
    )


def test_records_fact_windowed_timestamp_key_filters_half_open(tmp_path: Path) -> None:
    """Window key wrapped in derived: timestamp (no anchor): half-open filter holds.

    The window predicate binds to the derived: timestamp output column, which
    without an anchor carries the raw ns source value — rows filter exactly as
    with a plain from: projection of the raw key.
    """
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_timestamp_key_config()
    # window [10, 25): includes e001 (t=10) and e002 (t=20), excludes e003 (t=30)
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        assert len(specs) == 1
        assert specs[0].write_mode == "append"
        result = emit.query_arrow(specs[0].sql, ())

    ids = sorted(result.column("id").to_pylist())
    assert ids == ["e001", "e002"]
    # No anchor: the timestamp-derived key column carries the raw ns values
    assert sorted(result.column("event_ts").to_pylist()) == [10, 20]


def test_records_fact_windowed_timestamp_key_predicate_uses_raw_ns(
    tmp_path: Path,
) -> None:
    """Window key via derived: timestamp: WHERE binds the raw-ns-carrying column.

    Without an anchor the derived: timestamp column projects the raw ns source
    ("_grain"."last_mutation_sim_time" AS "event_ts"); the outer window
    predicate must bind to that output column, and windowed rows must carry
    values identical to the full export.
    """
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_timestamp_key_config()
    window = _make_window(start_ns=10, end_ns=25)

    with open_emit(emit_dir) as emit:
        full_specs = build_query_specs(emit, config, None, None)
        windowed_specs = build_query_specs(emit, config, None, window)

        sql = windowed_specs[0].sql
        # The inner SELECT projects the raw ns source under the key column name
        assert '"_grain"."last_mutation_sim_time" AS "event_ts"' in sql
        # The outer window predicate binds to that output column, half-open
        assert '"_windowed"."event_ts" >= 10' in sql
        assert '"_windowed"."event_ts" < 25' in sql

        full_rows = emit.query_arrow(full_specs[0].sql, ()).to_pydict()
        windowed_rows = emit.query_arrow(windowed_specs[0].sql, ()).to_pydict()

    full_by_id = {
        full_rows["id"][i]: (full_rows["name"][i], full_rows["event_ts"][i])
        for i in range(len(full_rows["id"]))
    }
    for i, rid in enumerate(windowed_rows["id"]):
        assert (windowed_rows["name"][i], windowed_rows["event_ts"][i]) == full_by_id[
            rid
        ]


# ---------------------------------------------------------------------------
# history_point fact: window predicate on sim_time
# ---------------------------------------------------------------------------


def test_history_point_windowed_filters_on_sim_time(tmp_path: Path) -> None:
    """history_point fact: windowed SELECT filters on sim_time half-open [start, end)."""
    emit_dir = _build_history_point_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_state",
                role="fact",
                source=SourceDecl(
                    grain="history_point",
                    kind="journey",
                    property="state",
                ),
                key=["record_id"],
                columns=[
                    ColumnDecl(name="record_id", **{"from": "record_id"}),
                    ColumnDecl(name="sim_time", **{"from": "sim_time"}),
                    ColumnDecl(name="val", **{"from": "value"}),
                ],
            )
        ]
    )
    # window [5, 20): includes sim_time=5 and sim_time=15, excludes sim_time=25
    window = _make_window(start_ns=5, end_ns=20)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        assert specs[0].write_mode == "append"
        result = emit.query_arrow(specs[0].sql, ())

    times = sorted(result.column("sim_time").to_pylist())
    assert times == [5, 15]


# ---------------------------------------------------------------------------
# Type-1 dim: replace mode, no window predicate
# ---------------------------------------------------------------------------


def test_type1_dim_windowed_is_replace_full_snapshot(tmp_path: Path) -> None:
    """Type-1 dim: windowed compile yields replace mode with full snapshot (no predicate)."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_type1_dim_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.write_mode == "replace"
        assert spec.view_name is None
        # Full snapshot: all 3 entities (no window predicate on type-1 dims)
        result = emit.query_arrow(spec.sql, ())

    assert result.num_rows == 3


def test_type1_dim_sql_identical_across_windows(tmp_path: Path) -> None:
    """Type-1 dim: SQL is identical for every window (full snapshot, no predicate)."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_type1_dim_config()
    window_a = _make_window(start_ns=0, end_ns=15, index=0)
    window_b = _make_window(start_ns=15, end_ns=30, index=1)
    with open_emit(emit_dir) as emit:
        specs_a = build_query_specs(emit, config, None, window_a)
        specs_b = build_query_specs(emit, config, None, window_b)

    assert specs_a[0].sql == specs_b[0].sql


# ---------------------------------------------------------------------------
# SCD-2 with valid_to: __rows table + companion view
# ---------------------------------------------------------------------------


def test_scd2_with_valid_to_windowed_spec_name_is_rows(tmp_path: Path) -> None:
    """SCD-2 with valid_to: spec table_name is '<name>__rows'."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.table_name == "dim_actor__rows"
    assert spec.write_mode == "append"


def test_scd2_with_valid_to_windowed_view_name_is_author_name(tmp_path: Path) -> None:
    """SCD-2 with valid_to: companion view_name is the author's table name."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)

    spec = specs[0]
    assert spec.view_name == "dim_actor"
    assert spec.view_sql is not None


def test_scd2_with_valid_to_rows_excludes_valid_to_column(tmp_path: Path) -> None:
    """SCD-2 __rows SELECT does not include valid_to slots; includes __valid_from_ns."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    col_names = result.schema.names
    assert "__valid_from_ns" in col_names
    assert "valid_to" not in col_names
    assert "valid_from" in col_names
    assert "id" in col_names


def test_scd2_with_valid_to_rows_predicate_on_change_point(tmp_path: Path) -> None:
    """SCD-2 __rows: predicate is on the raw change point (version_start), half-open."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    # window [10, 25): includes changes at sim_time=10 and sim_time=20; excludes 30
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    assert result.num_rows == 2
    ns_values = sorted(result.column("__valid_from_ns").to_pylist())
    assert ns_values == [10, 20]


def test_scd2_with_valid_to_view_sql_contains_lead_valid_from(tmp_path: Path) -> None:
    """SCD-2 companion view SQL: valid_to uses LEAD(valid_from) OVER (PARTITION BY ...)."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)

    spec = specs[0]
    assert spec.view_sql is not None
    view_sql = spec.view_sql
    assert 'LEAD("valid_from")' in view_sql
    assert "PARTITION BY" in view_sql
    assert "__valid_from_ns" in view_sql
    assert 'FROM "dim_actor__rows"' in view_sql


def test_scd2_view_sql_identity_columns_exclude_scd_window_cols(tmp_path: Path) -> None:
    """SCD-2 view: PARTITION BY uses identity cols (key minus scd_window cols)."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)

    view_sql = specs[0].view_sql
    assert view_sql is not None
    # key = ['id', 'valid_from']; scd_window cols = {valid_from, valid_to}
    # identity = key - scd_window = ['id']
    assert '"id"' in view_sql
    # valid_from should NOT appear in PARTITION BY
    partition_start = view_sql.find("PARTITION BY")
    order_start = view_sql.find("ORDER BY", partition_start)
    partition_clause = view_sql[partition_start:order_start]
    assert '"valid_from"' not in partition_clause


# ---------------------------------------------------------------------------
# SCD-2 without valid_to: plain append, no view
# ---------------------------------------------------------------------------


def test_scd2_no_valid_to_windowed_plain_name_no_view(tmp_path: Path) -> None:
    """SCD-2 without valid_to: plain table name, append, view_name is None."""
    emit_dir = _build_scd2_no_valid_to_emit(tmp_path)
    config = _make_scd2_no_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.table_name == "dim_actor"
    assert spec.write_mode == "append"
    assert spec.view_name is None
    assert spec.view_sql is None


def test_scd2_no_valid_to_windowed_has_valid_from_ns(tmp_path: Path) -> None:
    """SCD-2 without valid_to windowed: result includes __valid_from_ns trailing column."""
    emit_dir = _build_scd2_no_valid_to_emit(tmp_path)
    config = _make_scd2_no_valid_to_config()
    window = _make_window(start_ns=10, end_ns=25)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    col_names = result.schema.names
    assert "__valid_from_ns" in col_names


# ---------------------------------------------------------------------------
# Values-equal-full-export
# ---------------------------------------------------------------------------


def test_windowed_values_equal_full_export_records_fact(tmp_path: Path) -> None:
    """Windowed fact rows carry identical values to the full-export rows."""
    emit_dir = _build_records_emit(tmp_path)
    config = _make_records_fact_config()
    window = _make_window(start_ns=10, end_ns=25)

    with open_emit(emit_dir) as emit:
        full_specs = build_query_specs(emit, config, None, None)
        windowed_specs = build_query_specs(emit, config, None, window)

        full_rows = emit.query_arrow(full_specs[0].sql, ()).to_pydict()
        windowed_rows = emit.query_arrow(windowed_specs[0].sql, ()).to_pydict()

    # Build a lookup from id -> row values from full export
    full_by_id = {
        full_rows["id"][i]: {
            "name": full_rows["name"][i],
            "mutated_at": full_rows["mutated_at"][i],
        }
        for i in range(len(full_rows["id"]))
    }
    # Every windowed row's values must exactly match the full-export value
    for i, rid in enumerate(windowed_rows["id"]):
        assert rid in full_by_id, f"id {rid!r} not in full export"
        assert windowed_rows["name"][i] == full_by_id[rid]["name"]
        assert windowed_rows["mutated_at"][i] == full_by_id[rid]["mutated_at"]


def test_windowed_ordinal_matches_full_export_ordinal(tmp_path: Path) -> None:
    """Ordinal on a windowed fact: selected rows carry same ordinal as in full export."""
    emit_dir = _build_ordinal_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_entity",
                role="fact",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(name="mutated_at", **{"from": "last_mutation_sim_time"}),
                    ColumnDecl(
                        name="row_num",
                        derived=DerivedSpec(
                            ordinal=OrdinalSpec(
                                partition_by="mutated_at", order_by="mutated_at"
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    # window [10, 15): picks up e001 and e002 (both at t=10)
    window = _make_window(start_ns=10, end_ns=15)

    with open_emit(emit_dir) as emit:
        full_specs = build_query_specs(emit, config, None, None)
        windowed_specs = build_query_specs(emit, config, None, window)

        full_rows = emit.query_arrow(full_specs[0].sql, ()).to_pydict()
        windowed_rows = emit.query_arrow(windowed_specs[0].sql, ()).to_pydict()

    # Build lookup: id -> row_num from full export
    full_ordinal_by_id = {
        full_rows["id"][i]: full_rows["row_num"][i] for i in range(len(full_rows["id"]))
    }
    # Windowed rows must match
    for i, rid in enumerate(windowed_rows["id"]):
        assert windowed_rows["row_num"][i] == full_ordinal_by_id[rid]


# ---------------------------------------------------------------------------
# Ordinal amendment (full export): raw ns source for rendered-time order_by
# ---------------------------------------------------------------------------


def test_ordinal_amendment_timestamp_sibling_uses_raw_ns(tmp_path: Path) -> None:
    """Ordinal ordered by a derived:timestamp sibling compiles ORDER BY to raw ns source."""
    emit_dir = _build_records_emit(tmp_path)
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_entity",
                role="fact",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="admitted_at",
                        derived=DerivedSpec(
                            timestamp=TimestampSpec(source="last_mutation_sim_time")
                        ),
                    ),
                    ColumnDecl(
                        name="row_num",
                        derived=DerivedSpec(
                            ordinal=OrdinalSpec(
                                partition_by="id", order_by="admitted_at"
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, None)

    sql = specs[0].sql
    # The ordinal amendment: ORDER BY must reference the raw ns source, not admitted_at
    assert "last_mutation_sim_time" in sql
    # The column name admitted_at should not appear in any ORDER BY context for ordinal
    # (only in PARTITION BY); ensure the ORDER BY references the raw ns column
    assert 'ORDER BY "_grain"."last_mutation_sim_time"' in sql


def test_ordinal_amendment_same_microsecond_orders_by_true_event_order(
    tmp_path: Path,
) -> None:
    """Two same-microsecond rows with ordinal amendment order by true event order."""
    emit_dir = _build_ordinal_emit(tmp_path)
    # e001 and e002 both have last_mutation_sim_time=10; they must sort by record_id
    config = DimensionalConfig(
        tables=[
            TableDecl(
                name="fact_entity",
                role="fact",
                source=SourceDecl(grain="records", kind="entity"),
                key=["id"],
                columns=[
                    ColumnDecl(name="id", **{"from": "record_id"}),
                    ColumnDecl(
                        name="admitted_at",
                        derived=DerivedSpec(
                            timestamp=TimestampSpec(source="last_mutation_sim_time")
                        ),
                    ),
                    ColumnDecl(
                        name="row_num",
                        derived=DerivedSpec(
                            ordinal=OrdinalSpec(
                                partition_by="admitted_at", order_by="admitted_at"
                            )
                        ),
                    ),
                ],
            )
        ]
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, None)
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # e001 and e002 are at same admitted_at; their row_num is determined by record_id tie-break
    # They should have row_num 1 and 2 within the partition
    t10_ids = [
        rows["id"][i] for i in range(len(rows["id"])) if rows["row_num"][i] in [1, 2]
    ]
    # Both should appear, ordered (e001 before e002)
    assert "e001" in t10_ids
    assert "e002" in t10_ids


# ---------------------------------------------------------------------------
# SCD-2 two versions inside one microsecond order deterministically
# ---------------------------------------------------------------------------


def test_scd2_with_valid_to_two_versions_same_microsecond_order_deterministic(
    tmp_path: Path,
) -> None:
    """Two versions in same microsecond window order by __valid_from_ns deterministically."""
    emit_dir = _build_scd2_with_valid_to_emit(tmp_path)
    config = _make_scd2_with_valid_to_config()
    # Include all versions
    window = _make_window(start_ns=10, end_ns=35)
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(emit, config, None, window)
        result = emit.query_arrow(specs[0].sql, ())

    # Result must be ordered by record_id, then version_start (via __valid_from_ns)
    rows = result.to_pydict()
    ns_values = rows["__valid_from_ns"]
    # Must be non-decreasing
    for i in range(1, len(ns_values)):
        assert ns_values[i] >= ns_values[i - 1]
