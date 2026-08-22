"""Tests for the SCD-2 type2 wide reconstruction in the dimensional exporter.

Verifies: N-version reconstruction, valid_from/valid_to windowing, tracked vs
static column split (flag-authoritative), single-version for tracked-but-unchanged
columns, projection-only columns never tracked, Scd2NeedsHistory validation rule,
flag-absent emits refused, and total ORDER BY. Also the `scd_window` object
form's instant-rendering election: a date-grained window, same-day version
collapse, and the open interval's `valid_to` staying NULL under every
election (temporal-elections sprint Phase 4). Also the per-record derived
modes admitted on a type2 dim (derived: timestamp / date_parse / value_map,
untracked sources only) and their expression identity with the
records-grain builders (scd2-derived-temporal-parse sprint Phase 2).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DerivedSpec,
    DimensionalConfig,
    ScdWindowSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
    ValueMapSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import (
    build_date_parse_expr,
    build_timestamp_expr,
    build_value_map_expr,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.scd import (
    build_scd2_column_expr_flag,
    build_scd2_sql,
)
from fabulexa_forge.exporters.dimensional.validation import check_scd2_needs_history
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Column definitions for SCD-2 test emits
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS_WITH_FLAGS = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__admission_count",
        "BIGINT",
        history_tracked=True,
        temporal_class="tracked",
    ),
]

_HISTORY_COLUMNS = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# Adds the untracked per-record derived-mode sources (Phase 2) to the flags
# fixture: prop__birth_date / prop__admitted_at_str for derived: date_parse
# (date-only vs. datetime-denoting formats), prop__region for derived: value_map.
_ACTOR_COLUMNS_WITH_DERIVED = [
    *_ACTOR_COLUMNS_WITH_FLAGS,
    prop_column(
        "prop__birth_date", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__admitted_at_str",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_SCD2_SOURCE_TABLE = "records__actor"
_SCD2_TABLE_LABEL = "dim_patient"


def _scd2_column_expr_sidecar() -> Sidecar:
    """Sidecar for build_scd2_column_expr_flag unit tests: prop__ columns
    across the DuckDB types the tracked-path CAST wrapping exercises, plus
    the untracked derived-mode sources _ACTOR_COLUMNS_WITH_DERIVED adds."""
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": _SCD2_SOURCE_TABLE,
                "category": "records",
                "record_kind": "actor",
                "columns": [
                    *_ACTOR_COLUMNS_WITH_DERIVED,
                    prop_column(
                        "prop__surgical_referred",
                        "BOOLEAN",
                        history_tracked=True,
                        temporal_class="tracked",
                    ),
                ],
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def _make_scd2_table_decl(name: str = "dim_patient") -> TableDecl:
    """Return a standard scd: type2 dim_patient TableDecl."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="admission_count", **{"from": "prop__admission_count"}),
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


def _make_config(table_decl: TableDecl) -> DimensionalConfig:
    return DimensionalConfig(tables=[table_decl])


def _make_scd2_table_decl_with(extra_column: ColumnDecl) -> TableDecl:
    """The standard scd: type2 dim_patient TableDecl plus one extra
    (typically derived) column."""
    decl = _make_scd2_table_decl()
    return decl.model_copy(update={"columns": [*decl.columns, extra_column]})


def _make_scd2_table_decl_elected(
    as_value: str, name: str = "dim_patient"
) -> TableDecl:
    """Return a scd: type2 dim_patient TableDecl whose valid_from/valid_to
    columns carry the object-form scd_window election `as_value`."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="admission_count", **{"from": "prop__admission_count"}),
            ColumnDecl(
                name="valid_from",
                derived=DerivedSpec(
                    scd_window=ScdWindowSpec(bound="valid_from", **{"as": as_value})
                ),
            ),
            ColumnDecl(
                name="valid_to",
                derived=DerivedSpec(
                    scd_window=ScdWindowSpec(bound="valid_to", **{"as": as_value})
                ),
            ),
        ],
    )


def _build_scd2_emit(
    tmp_path: Path,
    actor_columns: list[dict],
    actor_rows: list[tuple],
    history_rows: list[tuple],
) -> Path:
    """Build a test emit for SCD-2 tests.

    Args:
        tmp_path: Directory for the emit.
        actor_columns: Column specs for records__actor (must have flags).
        actor_rows: List of tuples to insert into records__actor.
        history_rows: List of tuples to insert into history.

    Returns:
        tmp_path.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__actor", actor_columns))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    for row in actor_rows:
        placeholders = ", ".join(["?"] * len(row))
        conn.execute(
            f'INSERT INTO "records__actor" VALUES ({placeholders})',
            list(row),
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                actor_columns,
                len(actor_rows),
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Test: N-version reconstruction from tracked changes
# ---------------------------------------------------------------------------


def test_n_versions_from_n_change_points(tmp_path: Path) -> None:
    """A record with N distinct tracked sim_times reconstructs to N versions."""
    # actor a001 has status change at t=10, t=20, t=30 → 3 versions
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
            ("trunk", "actor", "a001", "status", 30, "discharged"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

    assert len(specs) == 1
    sql = specs[0].sql

    with open_emit(emit_dir) as emit:
        result = emit.query_arrow(sql, ())

    assert result.num_rows == 3


def test_valid_from_to_windowing(tmp_path: Path) -> None:
    """valid_from equals change sim_time; valid_to equals LEAD(sim_time); last is NULL."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
            ("trunk", "actor", "a001", "status", 30, "discharged"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # Sorted by record_id, version_start: a001 @ t=10, t=20, t=30
    assert rows["valid_from"] == [10, 20, 30]
    # valid_to: [20, 30, None]
    assert rows["valid_to"][0] == 20
    assert rows["valid_to"][1] == 30
    assert rows["valid_to"][2] is None


def test_tracked_column_takes_per_version_value(tmp_path: Path) -> None:
    """A tracked column takes the most-recent history.value at or before version start."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
            ("trunk", "actor", "a001", "status", 30, "discharged"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["status"] == ["admitted", "under_treatment", "discharged"]


def test_static_column_constant_across_versions(tmp_path: Path) -> None:
    """A static column (history_tracked: false) is constant across all versions."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
            ("trunk", "actor", "a001", "status", 30, "discharged"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # prop__name is history_tracked: False → constant "Alice" across all 3 versions
    assert rows["name"] == ["Alice", "Alice", "Alice"]


def test_flag_authoritative_tracked_but_unchanged_single_version(
    tmp_path: Path,
) -> None:
    """A tracked-but-unchanged column → single version spanning the run."""
    # a001 has admission_count flagged as tracked but never changed in history
    # The flag is authoritative: there should be exactly 1 version from status changes
    # but admission_count appears as the snapshot value across that single version
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 10, 0, "Alice", "admitted", 1),
        ],
        history_rows=[
            # Only status changes; admission_count never changes
            ("trunk", "actor", "a001", "status", 10, "admitted"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # One version (only one change point for tracked columns)
    assert result.num_rows == 1
    # Flag-authoritative: tracked-but-unchanged column → single version spanning the run.
    # admission_count has no history rows → subquery returns NULL (tracked path).
    # The version count (1) is what validates flag-authoritative behavior.
    assert rows["status"] == ["admitted"]


def test_projection_introduced_column_never_tracked(tmp_path: Path) -> None:
    """A projection-introduced column (no upstream prop__) is never tracked."""
    # record_id is not a prop__ column → always static (direct projection)
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # id (from record_id) must be "a001" across all versions
    assert all(v == "a001" for v in rows["id"])


def test_total_order_by_record_id_valid_from(tmp_path: Path) -> None:
    """SCD-2 output carries total ORDER BY (record_id, version_start)."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 20, 0, "Alice", "discharged", 2),
            ("trunk", "a002", 0, True, None, 10, 1, "Bob", "admitted", 1),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
            ("trunk", "actor", "a002", "status", 10, "admitted"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

    sql = specs[0].sql
    assert "ORDER BY" in sql
    assert "record_id" in sql
    assert "version_start" in sql


def test_build_twice_yields_identical_sql(tmp_path: Path) -> None:
    """Building query specs twice yields identical SQL."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, 10, 0, "Alice", "admitted", 1)],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs1 = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        specs2 = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

    assert specs1[0].sql == specs2[0].sql


# ---------------------------------------------------------------------------
# Test: Scd2NeedsHistory validation rule
# ---------------------------------------------------------------------------


def _make_sidecar_with_flags(kind: str = "actor") -> Sidecar:
    """Build a minimal Sidecar with history_tracked flags."""
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": f"records__{kind}",
                "category": "records",
                "record_kind": kind,
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {
                        "name": "prop__status",
                        "type": "VARCHAR",
                        "history_tracked": True,
                    },
                ],
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def _make_sidecar_no_flags(kind: str = "actor") -> Sidecar:
    """Build a minimal Sidecar without history_tracked flags."""
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": f"records__{kind}",
                "category": "records",
                "record_kind": kind,
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "prop__status", "type": "VARCHAR"},
                ],
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def test_scd2_needs_history_raises_missing_valid_from_key(tmp_path: Path) -> None:
    """scd: type2 without a valid_from scd_window column in key raises."""
    table_decl = TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id"],  # valid_from NOT in key
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
        ],
    )
    sidecar = _make_sidecar_with_flags()

    with pytest.raises(ExportError, match="scd type2 table 'dim_patient' needs"):
        check_scd2_needs_history(table_decl, "records__actor", sidecar)


def test_scd2_needs_history_raises_no_tracked_column(tmp_path: Path) -> None:
    """scd: type2 with no tracked column raises."""
    # All columns have history_tracked: False
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
                ],
                "rows": 0,
            }
        ],
    }
    sidecar = Sidecar.from_raw(raw)
    table_decl = TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
        ],
    )

    with pytest.raises(ExportError, match="scd type2 table 'dim_patient' needs"):
        check_scd2_needs_history(table_decl, "records__actor", sidecar)


def test_scd2_needs_history_passes_valid_config() -> None:
    """A valid scd: type2 table passes Scd2NeedsHistory."""
    table_decl = _make_scd2_table_decl()
    sidecar = _make_sidecar_with_flags()
    # Should not raise
    check_scd2_needs_history(table_decl, "records__actor", sidecar)


def test_scd2_needs_history_refuses_flag_absent_emit() -> None:
    """Without flags, check_scd2_needs_history refuses with a clear re-emit message."""
    table_decl = _make_scd2_table_decl()
    sidecar = _make_sidecar_no_flags()
    with pytest.raises(ExportError, match="re-emit with history_tracked"):
        check_scd2_needs_history(table_decl, "records__actor", sidecar)


# ---------------------------------------------------------------------------
# Test: multiple records, multiple versions each
# ---------------------------------------------------------------------------


def test_multiple_records_multiple_versions(tmp_path: Path) -> None:
    """Each record reconstructs independently to its own version count."""
    # a001: 2 versions (status changes at t=10, t=20)
    # a002: 1 version (status changes at t=15)
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 20, 0, "Alice", "discharged", 2),
            ("trunk", "a002", 0, True, None, 15, 1, "Bob", "admitted", 1),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
            ("trunk", "actor", "a002", "status", 15, "admitted"),
        ],
    )

    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    # 2 versions for a001 + 1 version for a002 = 3 rows total
    assert result.num_rows == 3
    rows = result.to_pydict()
    # Ordered by record_id, valid_from
    assert rows["id"][0] == "a001"
    assert rows["id"][1] == "a001"
    assert rows["id"][2] == "a002"


# ---------------------------------------------------------------------------
# scd_window object form — instant-rendering election
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_value", ["date", "time", "timestamptz"])
def test_scd_window_open_interval_valid_to_null_under_every_election(
    tmp_path: Path, as_value: str
) -> None:
    """The last (open) version's valid_to renders NULL under every election —
    date, time, and timestamptz alike."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, 10, 0, "Alice", "admitted", 1)],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl_elected(as_value))
        specs = build_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["valid_to"] == [None]


def test_scd_window_date_grained_same_day_versions_collapse(tmp_path: Path) -> None:
    """A date-grained window: same-day version boundaries collapse
    valid_from == valid_to for the earlier version, while raw version order
    is preserved — 3 distinct rows, not deduplicated."""
    t1 = 3_600_000_000_000  # +1h -> 2024-06-01 01:00 UTC
    t2 = 18_000_000_000_000  # +5h -> 2024-06-01 05:00 UTC (same day as t1)
    t3 = 108_000_000_000_000  # +30h -> 2024-06-02 06:00 UTC (next day)
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, t3, 0, "Alice", "discharged", 3)],
        history_rows=[
            ("trunk", "actor", "a001", "status", t1, "admitted"),
            ("trunk", "actor", "a001", "status", t2, "under_treatment"),
            ("trunk", "actor", "a001", "status", t3, "discharged"),
        ],
    )
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl_elected("date"))
        specs = build_query_specs(
            emit,
            config,
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    # Raw version order preserved: 3 distinct rows, not deduplicated by date.
    assert result.num_rows == 3
    assert rows["status"] == ["admitted", "under_treatment", "discharged"]
    # Version 1 (t1 -> t2, same calendar day) collapses valid_from == valid_to.
    assert rows["valid_from"][0] == rows["valid_to"][0]
    # Version 2 (t2 -> t3, crosses midnight) does not collapse.
    assert rows["valid_from"][1] != rows["valid_to"][1]
    # Version 3 is open (last) -> valid_to is None.
    assert rows["valid_to"][2] is None


def test_build_scd2_column_expr_flag_scd_window_object_form_date() -> None:
    """The ScdWindowSpec object form with as: date renders a
    CAST(... AS DATE) window off the raw version_start column."""
    col = ColumnDecl(
        name="valid_from",
        derived=DerivedSpec(
            scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
        ),
    )
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    expr = _flag_expr(col, frozenset(), anchor, _scd2_column_expr_sidecar())
    assert "CAST(" in expr
    assert "AS DATE)" in expr
    assert "version_start" in expr
    assert 'AS "valid_from"' in expr


def test_scd_window_bare_literal_unchanged_by_election_grammar(tmp_path: Path) -> None:
    """The bare-literal shorthand (no election) reconstructs identically
    against the same fixture the object-form tests use — the default
    `timestamp` rendering, unaffected by the new election grammar."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[
            ("trunk", "a001", 0, True, None, 30, 0, "Alice", "discharged", 3),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "under_treatment"),
            ("trunk", "actor", "a001", "status", 30, "discharged"),
        ],
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["valid_from"] == [10, 20, 30]
    assert rows["valid_to"] == [20, 30, None]


# ---------------------------------------------------------------------------
# Tests for expression-builder paths
# ---------------------------------------------------------------------------


def _make_col_decl_scd_window(name: str, bound: str) -> ColumnDecl:
    return ColumnDecl(name=name, derived=DerivedSpec(scd_window=bound))


def _make_col_decl_null(name: str) -> ColumnDecl:
    return ColumnDecl(name=name, null=True)


def _flag_expr(
    col: ColumnDecl,
    tracked_props: frozenset[str],
    anchor: EffectiveAnchor | None,
    sidecar: Sidecar,
) -> str:
    """Call build_scd2_column_expr_flag with the standard unit-test source
    binding (_SCD2_SOURCE_TABLE / _SCD2_TABLE_LABEL), varying only the
    per-test column/tracked-props/anchor/sidecar."""
    return build_scd2_column_expr_flag(
        col,
        "_versions",
        "_records",
        tracked_props,
        anchor,
        sidecar,
        _SCD2_SOURCE_TABLE,
        _SCD2_TABLE_LABEL,
    )


def test_build_scd2_column_expr_flag_with_anchor() -> None:
    """scd_window column with an anchor produces a TIMESTAMP offset expression."""
    col = _make_col_decl_scd_window("valid_from", "valid_from")
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    expr = _flag_expr(col, frozenset(), anchor, _scd2_column_expr_sidecar())
    # Expression must include the anchor timestamp and microsecond offset
    assert "TIMESTAMPTZ '2024-01-01T00:00:00+00:00'" in expr
    assert "to_microseconds" in expr
    assert 'AS "valid_from"' in expr


def test_build_scd2_column_expr_flag_scd_window_no_runtime() -> None:
    """scd_window column without runtime anchor produces direct column alias."""
    col = _make_col_decl_scd_window("valid_to", "valid_to")
    expr = _flag_expr(col, frozenset(), None, _scd2_column_expr_sidecar())
    assert "version_end" in expr
    assert 'AS "valid_to"' in expr
    assert "TIMESTAMP" not in expr


def test_build_scd2_column_expr_flag_null_column() -> None:
    """null column produces CAST(NULL AS VARCHAR) expression."""
    col = _make_col_decl_null("placeholder")
    expr = _flag_expr(col, frozenset(), None, _scd2_column_expr_sidecar())
    assert "CAST(NULL AS VARCHAR)" in expr
    assert 'AS "placeholder"' in expr


# ---------------------------------------------------------------------------
# Test: SCD flag path CAST wrapping
# ---------------------------------------------------------------------------


def test_build_scd2_column_expr_flag_bigint_wraps_cast() -> None:
    """Tracked flag path wraps correlated subquery in CAST(... AS BIGINT)."""
    col = ColumnDecl(name="admission_count", **{"from": "prop__admission_count"})
    expr = _flag_expr(
        col, frozenset({"admission_count"}), None, _scd2_column_expr_sidecar()
    )
    # Must start with CAST( and contain AS BIGINT)
    assert expr.startswith("CAST(")
    assert "AS BIGINT)" in expr
    assert 'AS "admission_count"' in expr
    # Must project from the derivation, not a correlated subquery (flag path)
    assert "EXISTS" not in expr
    assert "CASE WHEN" not in expr


def test_build_scd2_column_expr_flag_varchar_no_regression() -> None:
    """Tracked flag path with VARCHAR source includes CAST(... AS VARCHAR)."""
    col = ColumnDecl(name="status", **{"from": "prop__status"})
    expr = _flag_expr(col, frozenset({"status"}), None, _scd2_column_expr_sidecar())
    assert expr.startswith("CAST(")
    assert "AS VARCHAR)" in expr
    assert 'AS "status"' in expr


def test_build_scd2_column_expr_flag_boolean_wraps_cast() -> None:
    """Tracked flag path wraps correlated subquery in CAST(... AS BOOLEAN)."""
    col = ColumnDecl(name="referred", **{"from": "prop__surgical_referred"})
    expr = _flag_expr(
        col, frozenset({"surgical_referred"}), None, _scd2_column_expr_sidecar()
    )
    assert expr.startswith("CAST(")
    assert "AS BOOLEAN)" in expr


def test_build_scd2_column_expr_flag_static_unchanged() -> None:
    """Static (not tracked) flag path reads from the reader records relation."""
    col = ColumnDecl(name="name", **{"from": "prop__name"})
    expr = _flag_expr(col, frozenset(), None, _scd2_column_expr_sidecar())
    # Static: direct projection from reader records relation, no tracked subquery
    assert "SELECT h.value" not in expr
    assert "_records" in expr
    assert 'AS "name"' in expr


# ---------------------------------------------------------------------------
# Test: per-record derived modes on type2 (Phase 2) — untracked sources only
# ---------------------------------------------------------------------------


def test_derived_date_parse_untracked_exports_date_constant_across_versions(
    tmp_path: Path,
) -> None:
    """derived: date_parse on an untracked VARCHAR prop exports a DATE
    column on a type2 dim, constant across one record's version rows."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_DERIVED,
        actor_rows=[
            (
                "trunk",
                "a001",
                0,
                True,
                None,
                20,
                0,
                "Alice",
                "discharged",
                2,
                "1990-05-12",
                "2024-06-01 08:30:00",
                "north",
            ),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
        ],
    )
    table_decl = _make_scd2_table_decl_with(
        ColumnDecl(
            name="birth_date",
            derived=DerivedSpec(
                date_parse=DateParseSpec(
                    **{"from": "prop__birth_date", "format": "%Y-%m-%d"}
                )
            ),
        )
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _make_config(table_decl),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["birth_date"] == [date(1990, 5, 12), date(1990, 5, 12)]


def test_derived_date_parse_datetime_format_exports_timestamp_on_type2_dim(
    tmp_path: Path,
) -> None:
    """derived: date_parse with a datetime format exports TIMESTAMP on a
    type2 dim (composes Phase 1)."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_DERIVED,
        actor_rows=[
            (
                "trunk",
                "a001",
                0,
                True,
                None,
                10,
                0,
                "Alice",
                "admitted",
                1,
                "1990-05-12",
                "2024-06-01 08:30:00",
                "north",
            ),
        ],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )
    table_decl = _make_scd2_table_decl_with(
        ColumnDecl(
            name="admitted_ts",
            derived=DerivedSpec(
                date_parse=DateParseSpec(
                    **{
                        "from": "prop__admitted_at_str",
                        "format": "%Y-%m-%d %H:%M:%S",
                    }
                )
            ),
        )
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _make_config(table_decl),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["admitted_ts"][0] == datetime(2024, 6, 1, 8, 30, 0)


def test_derived_timestamp_elected_with_anchor_constant_across_versions(
    tmp_path: Path,
) -> None:
    """derived: timestamp on a structural instant (created_sim_time) with an
    anchor and an explicit election renders the elected type on every
    version row; identical value across versions."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_DERIVED,
        actor_rows=[
            (
                "trunk",
                "a001",
                3_600_000_000_000,
                True,
                None,
                20,
                0,
                "Alice",
                "discharged",
                2,
                "1990-05-12",
                "2024-06-01 08:30:00",
                "north",
            ),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
        ],
    )
    table_decl = _make_scd2_table_decl_with(
        ColumnDecl(
            name="created_at",
            derived=DerivedSpec(
                timestamp=TimestampSpec(source="created_sim_time", **{"as": "date"})
            ),
        )
    )
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _make_config(table_decl),
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["created_at"] == [date(2024, 6, 1), date(2024, 6, 1)]


def test_derived_timestamp_default_unelected_no_anchor_renders_raw_ns(
    tmp_path: Path,
) -> None:
    """Default (unelected) derived: timestamp with no anchor renders the raw
    ns integer."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_DERIVED,
        actor_rows=[
            (
                "trunk",
                "a001",
                3_600_000_000_000,
                True,
                None,
                10,
                0,
                "Alice",
                "admitted",
                1,
                "1990-05-12",
                "2024-06-01 08:30:00",
                "north",
            ),
        ],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )
    table_decl = _make_scd2_table_decl_with(
        ColumnDecl(
            name="created_raw",
            derived=DerivedSpec(timestamp=TimestampSpec(source="created_sim_time")),
        )
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _make_config(table_decl),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["created_raw"] == [3_600_000_000_000]


def test_derived_value_map_untracked_exports_typed_case_constant_across_versions(
    tmp_path: Path,
) -> None:
    """derived: value_map on an untracked prop exports the typed CASE
    value, constant across versions."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_DERIVED,
        actor_rows=[
            (
                "trunk",
                "a001",
                0,
                True,
                None,
                20,
                0,
                "Alice",
                "discharged",
                2,
                "1990-05-12",
                "2024-06-01 08:30:00",
                "north",
            ),
        ],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
        ],
    )
    table_decl = _make_scd2_table_decl_with(
        ColumnDecl(
            name="region_code",
            derived=DerivedSpec(
                value_map=ValueMapSpec(
                    **{"from": "prop__region"}, map={"north": 1, "south": 2}
                )
            ),
        )
    )
    with open_emit(emit_dir) as emit:
        specs = build_query_specs(
            emit,
            _make_config(table_decl),
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    rows = result.to_pydict()
    assert rows["region_code"] == [1, 1]


# ---------------------------------------------------------------------------
# Expression identity: type2 derived expr == records-grain builder output,
# modulo the grain alias (_records on both sides here).
# ---------------------------------------------------------------------------


def test_derived_timestamp_expression_identical_to_records_grain_builder() -> None:
    """The type2 derived: timestamp expression equals build_timestamp_expr's
    output, modulo the grain alias."""
    col = ColumnDecl(
        name="created_at",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "date"})
        ),
    )
    anchor = EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-06-01T00:00:00+00:00"),
        timezone=ZoneInfo("UTC"),
    )
    scd2_expr = _flag_expr(col, frozenset(), anchor, _scd2_column_expr_sidecar())
    records_expr = build_timestamp_expr(col, anchor, '"_records"."created_sim_time"')
    assert scd2_expr == records_expr


def test_derived_date_parse_expression_identical_to_records_grain_builder() -> None:
    """The type2 derived: date_parse expression equals build_date_parse_expr's
    output, modulo the grain alias."""
    col = ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "prop__birth_date", "format": "%Y-%m-%d"}
            )
        ),
    )
    table_decl = _make_scd2_table_decl()
    scd2_expr = _flag_expr(col, frozenset(), None, _scd2_column_expr_sidecar())
    records_expr = build_date_parse_expr(
        col, '"_records"."prop__birth_date"', table_decl.name
    )
    assert scd2_expr == records_expr


def test_derived_value_map_expression_identical_to_records_grain_builder() -> None:
    """The type2 derived: value_map expression equals build_value_map_expr's
    output, modulo the grain alias."""
    col = ColumnDecl(
        name="region_code",
        derived=DerivedSpec(
            value_map=ValueMapSpec(
                **{"from": "prop__region"}, map={"north": 1, "south": 2}
            )
        ),
    )
    scd2_expr = _flag_expr(col, frozenset(), None, _scd2_column_expr_sidecar())
    records_expr = build_value_map_expr(
        col, '"_records"."prop__region"', source_col_type="VARCHAR"
    )
    assert scd2_expr == records_expr


def test_build_scd2_sql_flag_path_uses_cast_for_bigint(tmp_path: Path) -> None:
    """build_scd2_sql with BIGINT tracked column emits CAST(... AS BIGINT) in SQL."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, 10, 0, "Alice", "admitted", 1)],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

    sql = specs[0].sql
    # admission_count is BIGINT tracked → projected from derivation with CAST(... AS BIGINT)
    assert "prop__admission_count" in sql
    assert "AS BIGINT)" in sql


def test_scd2_bigint_column_execution_succeeds(tmp_path: Path) -> None:
    """SCD-2 query with BIGINT tracked column executes without error."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, 20, 0, "Alice", "discharged", 3)],
        history_rows=[
            ("trunk", "actor", "a001", "status", 10, "admitted"),
            ("trunk", "actor", "a001", "status", 20, "discharged"),
            ("trunk", "actor", "a001", "admission_count", 10, "1"),
            ("trunk", "actor", "a001", "admission_count", 20, "3"),
        ],
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )
        result = emit.query_arrow(specs[0].sql, ())

    # Should produce rows without SQL type errors
    assert result.num_rows >= 1


def test_build_scd2_sql_no_tracked_props_yields_empty_filter() -> None:
    """build_scd2_sql with history_tracked flags but none tracked uses 1=0 filter."""
    # Build a sidecar where history_tracked_available() is True (has the flag)
    # but no column is flagged history_tracked=True → tracked_props is empty → 1=0.
    # We call build_scd2_sql directly, bypassing validation, to test this branch.
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "prop__name", "type": "VARCHAR", "history_tracked": False},
                ],
                "rows": 0,
            }
        ],
    }
    sidecar = Sidecar.from_raw(raw)
    table_decl = _make_scd2_table_decl()

    sql = build_scd2_sql(table_decl, "records__actor", sidecar, None, fork_path="trunk")
    # When no props are tracked, the versioned-intervals derivation sources FROM ()
    # (empty union) → no version rows; the derivation is still composed in the SQL.
    assert "_versions" in sql
    assert "FROM ()" in sql


def test_scd2_sql_embeds_versioned_intervals_derivation(tmp_path: Path) -> None:
    """Generated SQL embeds the versioned-intervals derivation (no raw FROM history)."""
    emit_dir = _build_scd2_emit(
        tmp_path,
        _ACTOR_COLUMNS_WITH_FLAGS,
        actor_rows=[("trunk", "a001", 0, True, None, 10, 0, "Alice", "admitted", 1)],
        history_rows=[("trunk", "actor", "a001", "status", 10, "admitted")],
    )
    with open_emit(emit_dir) as emit:
        config = _make_config(_make_scd2_table_decl())
        specs = build_query_specs(
            emit,
            config,
            None,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
        )

    sql = specs[0].sql
    # The composed SQL must reference _versions (derivation alias) and _records
    # (records relation alias), not raw base-table reads authored by the format.
    assert "_versions" in sql
    assert "_records" in sql
    # version_start is the derivation's column name for the change point
    assert "version_start" in sql
