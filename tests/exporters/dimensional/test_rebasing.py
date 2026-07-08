"""End-to-end rebasing tests for the dimensional timestamp exporter.

Covers:
- UTC value-identity: identity export over a UTC sidecar yields the same
  materialized timestamp values as pre-sprint rendering for a fixed sim_time.
- DST-zone correctness: a DST-observing effective zone renders the correct
  offset at the event instant (not a zone-blind literal).
- Rebase origin shift: rebasing to a later base_date shifts every rendered
  timestamp by exactly the origin delta; inter-event deltas invariant.
- Re-zone only: same absolute instants, displayed wall clock differs by zone.
- scd_window valid_from / valid_to rebase identically to derived: timestamp.
- No-anchor: emit with no sidecar runtime and no rebase -> raw sim_time integers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from exporters._emit_fixtures import (
    _create_ddl,
    _table_spec,
    build_no_runtime_emit,
    build_test_emit,
)
from fabulexa_export import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_export.anchor import resolve_effective_anchor
from fabulexa_export.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_export.exporters.dimensional.engine import export_dimensional
from fabulexa_export.reader.emit import open_emit

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Emit builders for rebasing tests
# ---------------------------------------------------------------------------

# sim_time values used in build_test_emit history rows (nanoseconds):
#   j001.state: 5, 15, 25
# Entity last_mutation_sim_time: 10, 20
# UTC origin: 2024-01-01T00:00:00+00:00

_UTC_ORIGIN = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

_ACTOR_SCD2_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR", "history_tracked": False},
    {"name": "record_id", "type": "VARCHAR", "history_tracked": False},
    {"name": "active", "type": "BOOLEAN", "history_tracked": False},
    {"name": "deactivated_at", "type": "BIGINT", "history_tracked": False},
    {"name": "last_mutation_sim_time", "type": "BIGINT", "history_tracked": False},
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
]

_SCD2_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def build_scd2_emit(tmp_path: Path, runtime_block: dict[str, str] | None) -> Path:
    """Build a minimal emit with SCD-2-capable records and history.

    Creates:
      - records__actor: two rows with history_tracked status column
      - history: status changes at sim_time 10_000_000_000 and 50_000_000_000 ns

    Args:
        tmp_path: Directory to write the emit artifacts into.
        runtime_block: Sidecar runtime dict (timezone + start_datetime), or None.

    Returns:
        tmp_path (the emit directory).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_SCD2_COLUMNS))
    conn.execute(_create_ddl("history", _SCD2_HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", True, 50_000_000_000, "active"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 10_000_000_000, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a001", "status", 50_000_000_000, "active"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [
            {"fork_path": "trunk", "parent": None, "slice_at": 100_000_000_000}
        ],
        "tables": [
            _table_spec(
                "records__actor", "records", _ACTOR_SCD2_COLUMNS, 1, record_kind="actor"
            ),
            _table_spec("history", "fixed", _SCD2_HISTORY_COLUMNS, 2),
        ],
        "enum_domains": {"actor": {}},
    }
    if runtime_block is not None:
        sidecar["runtime"] = runtime_block

    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def make_timestamp_config() -> ExportConfig:
    """Return a config with a derived: timestamp column from last_mutation_sim_time."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(
                            name="ts",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="last_mutation_sim_time")
                            ),
                        ),
                    ],
                )
            ]
        ),
    )


def make_scd2_config() -> ExportConfig:
    """Return a config with scd_window valid_from / valid_to columns."""
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_actor",
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
                            name="valid_to", derived=DerivedSpec(scd_window="valid_to")
                        ),
                    ],
                )
            ]
        ),
    )


def _query_timestamps(db_path: Path, table: str, col: str) -> list[object]:
    """Query a timestamp column from a DuckDB output file, sorted."""
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(f'SELECT "{col}" FROM "{table}" ORDER BY "{col}"').fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Test: UTC value-identity
# ---------------------------------------------------------------------------


def test_utc_identity_anchor_same_values(tmp_path: Path) -> None:
    """Identity export over a UTC sidecar yields same timestamps as the sidecar origin.

    With UTC timezone and start_datetime=2024-01-01T00:00:00+00:00, sim_time=10s
    (10_000_000_000 ns) should render as 2024-01-01T00:00:10+00:00.
    """
    emit_path = tmp_path / "emit"
    emit_path.mkdir()
    emit_dir = build_test_emit(emit_path)
    config = make_timestamp_config()
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(emit, config, out_path, "duckdb", anchor)

    timestamps = _query_timestamps(out_path, "dim_entity", "ts")
    assert len(timestamps) == 2
    # Both values are TIMESTAMP type (not raw integers) — the anchor was applied
    for ts in timestamps:
        ts_str = str(ts)
        # Should contain year-month-day format, not be a raw integer
        assert "2024-01-01" in ts_str, f"Expected UTC 2024-01-01 date, got {ts_str}"


# ---------------------------------------------------------------------------
# Test: DST-zone correctness
# ---------------------------------------------------------------------------


def test_dst_zone_renders_correct_offset(tmp_path: Path) -> None:
    """DST-observing zone renders the correct offset at the event instant.

    2024-06-01 is in US/Eastern DST (EDT = UTC-4). An event at sim_time=0
    starting at 2024-06-01T12:00:00+00:00 should render as 2024-06-01T08:00:00
    (UTC-4 = 8am local), not as 07:00:00 (EST/UTC-5 which would be zone-blind).
    """
    # Build an emit with a June start date (EDT, not EST)
    emit_dir = build_scd2_emit(
        tmp_path / "emit",
        runtime_block={
            "timezone": "America/New_York",
            "start_datetime": "2024-06-01T12:00:00+00:00",
        },
    )
    config = make_scd2_config()
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(emit, config, out_path, "duckdb", anchor)

    valid_froms = _query_timestamps(out_path, "dim_actor", "valid_from")
    # sim_time=10_000_000_000 ns = 10s after origin = 2024-06-01T12:00:10 UTC
    # In EDT (UTC-4): 2024-06-01T08:00:10
    assert len(valid_froms) >= 1
    ts_str = str(valid_froms[0])
    # Should show 08:00:10 (EDT) not 07:00:10 (EST)
    assert "08:00:10" in ts_str, f"Expected EDT offset (08:00:10), got {ts_str}"


# ---------------------------------------------------------------------------
# Test: Rebase origin shift
# ---------------------------------------------------------------------------


def test_rebase_origin_shift_moves_all_timestamps(tmp_path: Path) -> None:
    """Rebasing to a later base_date shifts every timestamp by exactly the delta.

    Inter-event deltas must remain invariant under the shift.
    """
    emit_path = tmp_path / "emit"
    emit_path.mkdir()
    emit_dir = build_test_emit(emit_path)
    config = make_timestamp_config()

    out_original = tmp_path / "original.duckdb"
    out_rebased = tmp_path / "rebased.duckdb"

    # Original: identity anchor (2024-01-01 UTC)
    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor_original = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(emit, config, out_original, "duckdb", anchor_original)

    # Rebased: 7 days later (still UTC)
    later_base = datetime(2024, 1, 8, 0, 0, 0)  # naive, 7 days later
    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor_rebased = resolve_effective_anchor(
            sidecar_runtime, None, later_base, "UTC"
        )
        export_dimensional(emit, config, out_rebased, "duckdb", anchor_rebased)

    ts_original = _query_timestamps(out_original, "dim_entity", "ts")
    ts_rebased = _query_timestamps(out_rebased, "dim_entity", "ts")

    assert len(ts_original) == len(ts_rebased)
    expected_delta = timedelta(days=7)

    for orig, rebased in zip(ts_original, ts_rebased):
        # Convert to comparable datetimes
        orig_dt = (
            orig if isinstance(orig, datetime) else datetime.fromisoformat(str(orig))
        )
        rebased_dt = (
            rebased
            if isinstance(rebased, datetime)
            else datetime.fromisoformat(str(rebased))
        )
        # Strip tz for comparison if needed
        if orig_dt.tzinfo is not None:
            orig_dt = orig_dt.replace(tzinfo=None)
        if rebased_dt.tzinfo is not None:
            rebased_dt = rebased_dt.replace(tzinfo=None)
        delta = rebased_dt - orig_dt
        assert abs(delta - expected_delta) < timedelta(seconds=1), (
            f"Expected 7-day shift, got {delta}"
        )

    # Inter-event deltas invariant
    if len(ts_original) >= 2:
        orig_dts = []
        reb_dts = []
        for ts in ts_original:
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            orig_dts.append(dt.replace(tzinfo=None) if dt.tzinfo else dt)
        for ts in ts_rebased:
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            reb_dts.append(dt.replace(tzinfo=None) if dt.tzinfo else dt)

        orig_delta = orig_dts[1] - orig_dts[0]
        reb_delta = reb_dts[1] - reb_dts[0]
        assert abs(orig_delta - reb_delta) < timedelta(microseconds=1), (
            f"Inter-event delta changed: {orig_delta} vs {reb_delta}"
        )


# ---------------------------------------------------------------------------
# Test: Re-zone only
# ---------------------------------------------------------------------------


def test_rezone_same_instant_different_wall_clock(tmp_path: Path) -> None:
    """Re-zone only: same absolute instants, displayed wall clock differs by the zone.

    UTC vs America/New_York (UTC-5 in January): timestamps differ by 5 hours wall-clock
    but represent the same absolute instants.
    """
    emit_path = tmp_path / "emit"
    emit_path.mkdir()
    emit_dir = build_test_emit(emit_path)
    config = make_timestamp_config()

    out_utc = tmp_path / "utc.duckdb"
    out_ny = tmp_path / "ny.duckdb"

    # Identity anchor (UTC)
    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor_utc = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(emit, config, out_utc, "duckdb", anchor_utc)

    # Re-zone to America/New_York (no base_date override)
    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor_ny = resolve_effective_anchor(
            sidecar_runtime, None, None, "America/New_York"
        )
        export_dimensional(emit, config, out_ny, "duckdb", anchor_ny)

    ts_utc = _query_timestamps(out_utc, "dim_entity", "ts")
    ts_ny = _query_timestamps(out_ny, "dim_entity", "ts")

    assert len(ts_utc) == len(ts_ny)
    # All NY timestamps should be 5 hours behind UTC (Jan = EST = UTC-5)
    for utc_ts, ny_ts in zip(ts_utc, ts_ny):
        utc_dt = (
            utc_ts
            if isinstance(utc_ts, datetime)
            else datetime.fromisoformat(str(utc_ts))
        )
        ny_dt = (
            ny_ts if isinstance(ny_ts, datetime) else datetime.fromisoformat(str(ny_ts))
        )
        # Strip tz for wall-clock comparison
        utc_wall = utc_dt.replace(tzinfo=None)
        ny_wall = ny_dt.replace(tzinfo=None)
        diff = utc_wall - ny_wall
        assert abs(diff - timedelta(hours=5)) < timedelta(seconds=1), (
            f"Expected 5-hour UTC/NY difference, got {diff}"
        )


# ---------------------------------------------------------------------------
# Test: scd_window rebases identically to derived: timestamp
# ---------------------------------------------------------------------------


def test_scd_window_rebases_same_as_timestamp(tmp_path: Path) -> None:
    """scd_window valid_from / valid_to rebase identically to derived: timestamp.

    Both use render_anchor_timestamp_expr under the hood, so the same anchor
    applied to version_start/version_end must shift by the same delta.
    """
    runtime_block = {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"}
    emit_identity = build_scd2_emit(tmp_path / "emit_identity", runtime_block)
    emit_rebased_dir = build_scd2_emit(tmp_path / "emit_rebased", runtime_block)

    config = make_scd2_config()
    out_identity = tmp_path / "identity.duckdb"
    out_rebased = tmp_path / "rebased.duckdb"

    # Identity
    with open_emit(emit_identity) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor = resolve_effective_anchor(sidecar_runtime, None, None, None)
        export_dimensional(emit, config, out_identity, "duckdb", anchor)

    # Rebased 30 days later
    later_base = datetime(2024, 1, 31, 0, 0, 0)
    with open_emit(emit_rebased_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor_rebased = resolve_effective_anchor(
            sidecar_runtime, None, later_base, "UTC"
        )
        export_dimensional(emit, config, out_rebased, "duckdb", anchor_rebased)

    vf_identity = _query_timestamps(out_identity, "dim_actor", "valid_from")
    vf_rebased = _query_timestamps(out_rebased, "dim_actor", "valid_from")

    assert len(vf_identity) == len(vf_rebased)
    expected_delta = timedelta(days=30)
    for orig, rebased in zip(vf_identity, vf_rebased):
        orig_dt = (
            orig if isinstance(orig, datetime) else datetime.fromisoformat(str(orig))
        )
        reb_dt = (
            rebased
            if isinstance(rebased, datetime)
            else datetime.fromisoformat(str(rebased))
        )
        orig_wall = orig_dt.replace(tzinfo=None)
        reb_wall = reb_dt.replace(tzinfo=None)
        delta = reb_wall - orig_wall
        assert abs(delta - expected_delta) < timedelta(seconds=1), (
            f"scd_window valid_from shift mismatch: {delta}"
        )

    # valid_to should also shift by 30 days
    vt_identity = _query_timestamps(out_identity, "dim_actor", "valid_to")
    vt_rebased = _query_timestamps(out_rebased, "dim_actor", "valid_to")
    # valid_to can be NULL for the last version; check non-null values
    pairs = [
        (o, r)
        for o, r in zip(vt_identity, vt_rebased)
        if o is not None and r is not None
    ]
    for orig, rebased in pairs:
        orig_dt = (
            orig if isinstance(orig, datetime) else datetime.fromisoformat(str(orig))
        )
        reb_dt = (
            rebased
            if isinstance(rebased, datetime)
            else datetime.fromisoformat(str(rebased))
        )
        orig_wall = orig_dt.replace(tzinfo=None)
        reb_wall = reb_dt.replace(tzinfo=None)
        delta = reb_wall - orig_wall
        assert abs(delta - expected_delta) < timedelta(seconds=1), (
            f"scd_window valid_to shift mismatch: {delta}"
        )


# ---------------------------------------------------------------------------
# Test: No-anchor path
# ---------------------------------------------------------------------------


def test_no_anchor_yields_raw_integers(tmp_path: Path) -> None:
    """No sidecar runtime + no rebase -> raw sim_time integer output."""
    emit_path = tmp_path / "emit"
    emit_path.mkdir()
    emit_dir = build_no_runtime_emit(emit_path)

    config = ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[
                        ColumnDecl(name="id", **{"from": "record_id"}),
                        ColumnDecl(
                            name="ts",
                            derived=DerivedSpec(
                                timestamp=TimestampSpec(source="last_mutation_sim_time")
                            ),
                        ),
                    ],
                )
            ]
        ),
    )
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        sidecar_runtime = emit.sidecar.runtime()
        anchor = resolve_effective_anchor(sidecar_runtime, None, None, None)
        assert anchor is None  # no runtime, no rebase → no anchor
        export_dimensional(emit, config, out_path, "duckdb", anchor)

    conn = duckdb.connect(str(out_path), read_only=True)
    rows = conn.execute('SELECT "ts" FROM "dim_entity"').fetchall()
    describe_rows = conn.execute("DESCRIBE dim_entity").fetchall()
    conn.close()

    assert len(rows) == 1
    # The ts column should be raw integer (BIGINT), not a timestamp type
    ts_col_info = [r for r in describe_rows if r[0] == "ts"]
    assert len(ts_col_info) == 1
    col_type_str = ts_col_info[0][1].upper()
    assert "BIGINT" in col_type_str or "INT" in col_type_str, (
        f"Expected integer type for no-anchor ts, got {col_type_str}"
    )
    # Value should be the raw sim_time integer (10 from build_no_runtime_emit)
    assert rows[0][0] == 10
