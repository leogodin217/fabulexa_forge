"""Tests for the `date_ref` column mode (dimensional exporter).

Covers `render_date_ref_expr` (both shapes' SQL fragment, the no-anchor
assertion), family agreement with the sibling renderers (`derived: timestamp`
/ `derived: date_parse`) across grains and anchors, NULL propagation, the
parse shape's loud non-matching-cell failure, SCD-2 tracked-vs-untracked
source-class reading, the ordinal amendment's sibling reference, and
determinism across repeated compiles.

Business-rule refusals over `date_ref` (TemporalRenderRequiresAnchor,
TimestampSourceAvailable, ProjectionColumnExists, DateParseSourceColumn,
SliceOnlyColumnRefused, Scd2ColumnModeSupported) live in
`test_validation.py`; window-delivery-class and KeyColumnsStable readings in
`test_windowing.py`; the admitted-mode parametrize in
`test_scd2_source_filter.py`; provenance and `QuerySpec.references` in
`test_provenance.py`.
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
from fabulexa_forge._sql import date_key_expr, date_parse_expr
from fabulexa_forge.anchor import EffectiveAnchor, anchor_temporal_expr
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DateRefSpec,
    DerivedSpec,
    DimensionalConfig,
    OrdinalSpec,
    ScdWindowSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.exporters.dimensional.columns import (
    build_column_expr,
    build_ordinal_expr,
    render_date_ref_expr,
    resolve_date_ref_source,
)
from fabulexa_forge.exporters.dimensional.engine import build_query_specs
from fabulexa_forge.exporters.dimensional.scd import build_scd2_column_expr_flag
from fabulexa_forge.exporters.dimensional.validation import check_ordinal_refs_siblings
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError
from fabulexa_forge.reader.sidecar import Sidecar

_DAY_NS = 86_400 * 1_000_000_000
_HOUR_NS = 3_600 * 1_000_000_000


def _anchor(zone: str) -> EffectiveAnchor:
    """A fixed 2024-01-01T00:00:00Z anchor, localized to `zone`."""
    return EffectiveAnchor(
        start_instant=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
        timezone=ZoneInfo(zone),
    )


_UTC = _anchor("UTC")

# ---------------------------------------------------------------------------
# Fixture emit: two patients (one created 2h into the anchor day -- crosses
# local midnight under America/New_York), a status interval series.
# ---------------------------------------------------------------------------

_PATIENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__dob", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    {"name": "kind", "type": "VARCHAR"},
    identity_column("record_id", "VARCHAR"),
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_date_ref_emit(tmp_path: Path) -> Path:
    """p001: created 2h into the anchor day (crosses local midnight under
    America/New_York), active, dob 1990-05-14, three status rows (last
    open). p002: created 4 days in, deactivated at 7 days, dob NULL, two
    status rows (last open)."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 2 * 3_600_000_000_000, True, None, 0, 0, "1990-05-14"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 4 * _DAY_NS, False, 7 * _DAY_NS, 7 * _DAY_NS, 1, None],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    status_rows = [
        ("trunk", "patient", "p001", "status", 1 * _DAY_NS, "waiting"),
        ("trunk", "patient", "p001", "status", 2 * _DAY_NS, "in_progress"),
        ("trunk", "patient", "p001", "status", 3 * _DAY_NS, "completed"),
        ("trunk", "patient", "p002", "status", 5 * _DAY_NS, "waiting"),
        ("trunk", "patient", "p002", "status", 6 * _DAY_NS, "in_progress"),
    ]
    for row in status_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__patient",
                "records",
                _PATIENT_COLUMNS,
                2,
                record_kind="patient",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(status_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 10 * _DAY_NS}],
    )
    return tmp_path


def _build_bad_dob_emit(tmp_path: Path) -> Path:
    """One patient whose dob does not match the declared parse format."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "not-a-date"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__patient",
                "records",
                _PATIENT_COLUMNS,
                1,
                record_kind="patient",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
    )
    return tmp_path


def _compile_and_run(
    emit_dir: Path,
    table_decl: TableDecl,
    anchor: EffectiveAnchor | None = None,
) -> dict[str, list[object]]:
    """Compile one table_decl's full-export query and run it, keyed by
    output column name."""
    with open_emit(emit_dir) as emit:
        (spec,) = build_query_specs(
            emit,
            DimensionalConfig(tables=[table_decl]),
            anchor,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            tables=None,
        )
        return emit.query_arrow(spec.sql, ()).to_pydict()


# ---------------------------------------------------------------------------
# resolve_date_ref_source: the one base column, or None for the bound shape
# ---------------------------------------------------------------------------


def test_resolve_date_ref_source_instant_and_parse_shapes_return_declared_name() -> (
    None
):
    """The instant shape returns `source`; the parse shape returns `from_`."""
    assert resolve_date_ref_source(DateRefSpec(source="sim_time")) == "sim_time"
    assert (
        resolve_date_ref_source(DateRefSpec(from_="dob_text", format="%Y-%m-%d"))
        == "dob_text"
    )


@pytest.mark.parametrize("bound", ["valid_from", "valid_to"])
def test_resolve_date_ref_source_bound_shape_returns_none(bound: str) -> None:
    """The bound shape reads no base column -- its input is the SCD-2
    version relation's bound, not a source column."""
    assert resolve_date_ref_source(DateRefSpec(scd_window=bound)) is None


# ---------------------------------------------------------------------------
# render_date_ref_expr: the SQL fragment, all three shapes
# ---------------------------------------------------------------------------


def test_render_date_ref_expr_instant_shape_composes_shared_renderers() -> None:
    """The instant shape composes `anchor_temporal_expr(..., "date")` then
    `date_key_expr`, aliased to `out_name`."""
    spec = DateRefSpec(source="created_sim_time")
    qualified_source = '"_grain"."created_sim_time"'
    expr = render_date_ref_expr(spec, qualified_source, "event_key", "fact_x", _UTC)

    date_sql = anchor_temporal_expr(_UTC, qualified_source, "date")
    assert expr == f'{date_key_expr(date_sql)} AS "event_key"'


def test_render_date_ref_expr_parse_shape_composes_shared_renderers() -> None:
    """The parse shape composes `date_parse_expr` cast to DATE then
    `date_key_expr`, aliased to `out_name`."""
    spec = DateRefSpec(from_="prop__dob", format="%Y-%m-%d")
    qualified_source = '"_grain"."prop__dob"'
    expr = render_date_ref_expr(spec, qualified_source, "dob_key", "dim_patient", None)

    date_sql = (
        f"CAST({date_parse_expr(qualified_source, '%Y-%m-%d', 'dim_patient')} AS DATE)"
    )
    assert expr == f'{date_key_expr(date_sql)} AS "dob_key"'


def test_render_date_ref_expr_instant_shape_with_no_anchor_asserts() -> None:
    """The instant shape asserts a resolved anchor -- the caller enforces
    TemporalRenderRequiresAnchor before this ever runs."""
    spec = DateRefSpec(source="created_sim_time")
    with pytest.raises(AssertionError):
        render_date_ref_expr(
            spec, '"_grain"."created_sim_time"', "event_key", "fx", None
        )


def test_render_date_ref_expr_bound_shape_composes_shared_renderers() -> None:
    """The bound shape composes `anchor_temporal_expr(..., "date")` then
    `date_key_expr` over the version relation's bound column, exactly as the
    instant shape does over a source column."""
    spec = DateRefSpec(scd_window="valid_from")
    qualified_source = '"_versions"."version_start"'
    expr = render_date_ref_expr(
        spec, qualified_source, "valid_from_date_key", "dim_company", _UTC
    )

    date_sql = anchor_temporal_expr(_UTC, qualified_source, "date")
    assert expr == f'{date_key_expr(date_sql)} AS "valid_from_date_key"'


def test_render_date_ref_expr_bound_shape_with_no_anchor_asserts() -> None:
    """The bound shape asserts a resolved anchor, exactly as the instant
    shape does."""
    spec = DateRefSpec(scd_window="valid_to")
    with pytest.raises(AssertionError):
        render_date_ref_expr(
            spec, '"_versions"."version_end"', "valid_to_date_key", "dim_company", None
        )


# ---------------------------------------------------------------------------
# Family agreement — records grain, instant shape (both anchors)
# ---------------------------------------------------------------------------


def _fact_created_date_table_decl() -> TableDecl:
    return TableDecl(
        name="fact_patient",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id"],
        columns=[
            ColumnDecl(name="patient_id", **{"from": "record_id"}),
            ColumnDecl(
                name="event_ts",
                derived=DerivedSpec(
                    timestamp=TimestampSpec(source="created_sim_time", as_="date")
                ),
            ),
            ColumnDecl(
                name="event_date_key", date_ref=DateRefSpec(source="created_sim_time")
            ),
        ],
    )


def _date_key(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


@pytest.mark.parametrize("zone", ["UTC", "America/New_York"])
def test_date_ref_instant_agrees_with_derived_timestamp_sibling(
    tmp_path: Path, zone: str
) -> None:
    """Every row's `date_ref` instant key equals `date_key_expr` over its
    `derived: timestamp {as: date}` sibling, under UTC and under a zone
    whose local date differs from the UTC date for one row."""
    emit_dir = _build_date_ref_emit(tmp_path)
    rows = _compile_and_run(emit_dir, _fact_created_date_table_decl(), _anchor(zone))

    for event_ts, event_date_key in zip(rows["event_ts"], rows["event_date_key"]):
        assert event_date_key == _date_key(event_ts)


def test_date_ref_instant_crosses_local_midnight_under_new_york(tmp_path: Path) -> None:
    """p001 (created 2h into the anchor day) renders a different date under
    America/New_York than under UTC -- the family agrees under either zone,
    but the zones themselves genuinely differ."""
    emit_dir = _build_date_ref_emit(tmp_path)
    utc_rows = _compile_and_run(
        emit_dir, _fact_created_date_table_decl(), _anchor("UTC")
    )
    ny_rows = _compile_and_run(
        emit_dir, _fact_created_date_table_decl(), _anchor("America/New_York")
    )
    utc_by_id = dict(zip(utc_rows["patient_id"], utc_rows["event_date_key"]))
    ny_by_id = dict(zip(ny_rows["patient_id"], ny_rows["event_date_key"]))
    assert utc_by_id["p001"] != ny_by_id["p001"]


# ---------------------------------------------------------------------------
# Family agreement — records grain, parse shape
# ---------------------------------------------------------------------------


def _dim_patient_dob_table_decl() -> TableDecl:
    return TableDecl(
        name="dim_patient",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(
                name="dob",
                derived=DerivedSpec(
                    date_parse=DateParseSpec(from_="prop__dob", format="%Y-%m-%d")
                ),
            ),
            ColumnDecl(
                name="dob_key",
                date_ref=DateRefSpec(from_="prop__dob", format="%Y-%m-%d"),
            ),
        ],
    )


def test_date_ref_parse_agrees_with_derived_date_parse_sibling(tmp_path: Path) -> None:
    """`date_ref {from, format}` equals `date_key_expr` over the sibling
    `derived: date_parse {from, format}`; 1990-05-14 -> 19900514."""
    emit_dir = _build_date_ref_emit(tmp_path)
    rows = _compile_and_run(emit_dir, _dim_patient_dob_table_decl())
    by_id = dict(zip(rows["id"], zip(rows["dob"], rows["dob_key"])))

    p001_dob, p001_dob_key = by_id["p001"]
    assert p001_dob == date(1990, 5, 14)
    assert p001_dob_key == 19900514

    for dob, dob_key in by_id.values():
        assert dob_key == (_date_key(dob) if dob is not None else None)


# ---------------------------------------------------------------------------
# NULL sources: both shapes, and the open-interval instant
# ---------------------------------------------------------------------------


def test_date_ref_instant_deactivated_at_null_for_active_record(tmp_path: Path) -> None:
    """`date_ref {source: deactivated_at}` on an active record (deactivated_at
    NULL) is NULL; a deactivated record's key is non-NULL."""
    emit_dir = _build_date_ref_emit(tmp_path)
    table_decl = TableDecl(
        name="fact_deactivation",
        role="fact",
        source=SourceDecl(grain="records", kind="patient"),
        key=["patient_id"],
        columns=[
            ColumnDecl(name="patient_id", **{"from": "record_id"}),
            ColumnDecl(
                name="deactivated_key", date_ref=DateRefSpec(source="deactivated_at")
            ),
        ],
    )
    rows = _compile_and_run(emit_dir, table_decl, _UTC)
    by_id = dict(zip(rows["patient_id"], rows["deactivated_key"]))
    assert by_id["p001"] is None
    assert by_id["p002"] is not None


def test_date_ref_instant_lead_sim_time_null_on_open_interval(tmp_path: Path) -> None:
    """`date_ref {source: lead_sim_time}` is NULL on each patient's still-open
    final status row, non-NULL on its earlier (closed) rows."""
    emit_dir = _build_date_ref_emit(tmp_path)
    table_decl = TableDecl(
        name="fact_status_event",
        role="fact",
        source=SourceDecl(grain="history_interval", kind="patient", property="status"),
        key=["record_id", "sim_time"],
        columns=[
            ColumnDecl(name="record_id", **{"from": "record_id"}),
            ColumnDecl(name="sim_time", **{"from": "sim_time"}),
            ColumnDecl(
                name="next_date_key", date_ref=DateRefSpec(source="lead_sim_time")
            ),
        ],
    )
    rows = _compile_and_run(emit_dir, table_decl, _UTC)
    by_patient: dict[str, list[tuple[int, object]]] = {}
    for record_id, sim_time, next_key in zip(
        rows["record_id"], rows["sim_time"], rows["next_date_key"]
    ):
        by_patient.setdefault(record_id, []).append((sim_time, next_key))

    for entries in by_patient.values():
        entries.sort(key=lambda e: e[0])
        *earlier, last = entries
        assert last[1] is None
        assert all(key is not None for _sim_time, key in earlier)


def test_date_ref_parse_null_prop_string_is_null(tmp_path: Path) -> None:
    """A NULL `prop__` string under the parse shape renders NULL."""
    emit_dir = _build_date_ref_emit(tmp_path)
    rows = _compile_and_run(emit_dir, _dim_patient_dob_table_decl())
    by_id = dict(zip(rows["id"], rows["dob_key"]))
    assert by_id["p002"] is None


# ---------------------------------------------------------------------------
# Parse shape: a non-matching cell fails the export loudly, naming the table
# ---------------------------------------------------------------------------


def test_date_ref_parse_non_matching_cell_fails_loudly_naming_table(
    tmp_path: Path,
) -> None:
    """A parse-shape source that fails the declared format fails the export
    loudly, the message naming the output table."""
    emit_dir = _build_bad_dob_emit(tmp_path)
    with pytest.raises(RunDatabaseError, match="dim_patient") as exc_info:
        _compile_and_run(emit_dir, _dim_patient_dob_table_decl())
    assert "does not match format" in str(exc_info.value)


# ---------------------------------------------------------------------------
# SCD-2: source-class reading (tracked reads per version, untracked per
# record) -- the SQL-composition level, mirroring test_scd.py's own unit
# style for build_scd2_column_expr_flag.
# ---------------------------------------------------------------------------

_SCD_SOURCE_TABLE = "records__patient"
_SCD_TABLE_LABEL = "dim_patient"


def _scd_sidecar() -> Sidecar:
    """A records__patient sidecar with one tracked VARCHAR date-string
    property, for build_scd2_column_expr_flag's source-class unit tests."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": _SCD_SOURCE_TABLE,
                "category": "records",
                "record_kind": "patient",
                "columns": [
                    identity_column("record_id", "VARCHAR"),
                    {"name": "created_sim_time", "type": "BIGINT"},
                    prop_column(
                        "prop__checkin_date",
                        "VARCHAR",
                        history_tracked=True,
                        temporal_class="tracked",
                    ),
                ],
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def _flag_expr(
    col: ColumnDecl,
    tracked_props: frozenset[str],
    anchor: EffectiveAnchor | None,
) -> str:
    """Call build_scd2_column_expr_flag with the standard unit-test source
    binding (_SCD_SOURCE_TABLE / _SCD_TABLE_LABEL)."""
    return build_scd2_column_expr_flag(
        col,
        "_versions",
        "_records",
        tracked_props,
        anchor,
        _scd_sidecar(),
        _SCD_SOURCE_TABLE,
        _SCD_TABLE_LABEL,
    )


def test_scd2_date_ref_parse_shape_tracked_source_reads_per_version() -> None:
    """A `date_ref` parse shape over a *tracked* VARCHAR property reads from
    the versioned-intervals alias -- a version's own value, so two versions
    of the same record carry two distinct keys."""
    col = ColumnDecl(
        name="checkin_key",
        date_ref=DateRefSpec(from_="prop__checkin_date", format="%Y-%m-%d"),
    )
    expr = _flag_expr(col, frozenset({"checkin_date"}), None)
    assert '"_versions"."prop__checkin_date"' in expr
    assert '"_records"' not in expr
    assert 'AS "checkin_key"' in expr


def test_scd2_date_ref_instant_shape_untracked_source_reads_per_record() -> None:
    """A `date_ref` instant shape over `created_sim_time` (structural, never
    tracked) reads from the records-relation alias -- the same key on every
    version of a record."""
    col = ColumnDecl(
        name="created_key", date_ref=DateRefSpec(source="created_sim_time")
    )
    expr = _flag_expr(col, frozenset({"checkin_date"}), _UTC)
    assert '"_records"."created_sim_time"' in expr
    assert '"_versions"' not in expr
    assert 'AS "created_key"' in expr


def test_scd2_date_ref_bound_shape_valid_from_reads_version_start() -> None:
    """The bound shape's `valid_from` reads `"_versions"."version_start"` --
    the branch runs before any source-column resolution, so a tracked-props
    set that would otherwise route source-class reading never matters."""
    col = ColumnDecl(
        name="valid_from_date_key", date_ref=DateRefSpec(scd_window="valid_from")
    )
    expr = _flag_expr(col, frozenset({"checkin_date"}), _UTC)
    assert '"_versions"."version_start"' in expr
    assert '"_records"' not in expr
    assert 'AS "valid_from_date_key"' in expr


def test_scd2_date_ref_bound_shape_valid_to_reads_version_end() -> None:
    """The bound shape's `valid_to` reads `"_versions"."version_end"`."""
    col = ColumnDecl(
        name="valid_to_date_key", date_ref=DateRefSpec(scd_window="valid_to")
    )
    expr = _flag_expr(col, frozenset(), _UTC)
    assert '"_versions"."version_end"' in expr
    assert '"_records"' not in expr
    assert 'AS "valid_to_date_key"' in expr


# ---------------------------------------------------------------------------
# The ordinal amendment: a date_ref column as partition_by / order_by sibling
# ---------------------------------------------------------------------------


def test_ordinal_partition_and_order_by_date_ref_column_compiles() -> None:
    """`ordinal.partition_by` naming a `date_ref` column compiles;
    `order_by` naming one orders by its rendered value, record_id
    tie-broken."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    date_key_col = ColumnDecl(
        name="dob_key", date_ref=DateRefSpec(from_="prop__dob", format="%Y-%m-%d")
    )
    ordinal_col = ColumnDecl(
        name="seq",
        derived=DerivedSpec(
            ordinal=OrdinalSpec(partition_by="dob_key", order_by="dob_key")
        ),
    )
    tbl = TableDecl(
        name="dim_patient",
        role="dim",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[id_col, date_key_col, ordinal_col],
    )
    check_ordinal_refs_siblings(ordinal_col, tbl)  # must not raise

    expr = build_ordinal_expr(ordinal_col, table_decl=tbl)
    assert 'PARTITION BY "dob_key"' in expr
    assert 'ORDER BY "dob_key", "_grain"."record_id"' in expr


# ---------------------------------------------------------------------------
# Bound shape end-to-end: an scd: type2 dim keying version rows into
# dim_date by valid_from / valid_to.
# ---------------------------------------------------------------------------

_COMPANY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]


def _build_scd_window_emit(tmp_path: Path) -> Path:
    """c001: three prop__status changes -- the first two 2h apart (the
    first crosses local midnight relative to UTC under America/New_York,
    and both land on the same New_York local date), the third stays open.
    c002: one (open) version."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__company", _COMPANY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__company" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "c001", 0, True, None, 0, 0, "pending"],
    )
    conn.execute(
        'INSERT INTO "records__company" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "c002", 0, True, None, 0, 1, "pending"],
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    status_rows = [
        ("trunk", "company", "c001", "status", 2 * _HOUR_NS, "active"),
        ("trunk", "company", "c001", "status", 4 * _HOUR_NS, "suspended"),
        ("trunk", "company", "c001", "status", 30 * _HOUR_NS, "active"),
        ("trunk", "company", "c002", "status", 10 * _HOUR_NS, "active"),
    ]
    for row in status_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__company",
                "records",
                _COMPANY_COLUMNS,
                2,
                record_kind="company",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, len(status_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 40 * _HOUR_NS}],
    )
    return tmp_path


def _type2_dim_company_table_decl(*, with_valid_to: bool) -> TableDecl:
    """`dim_company`: the `valid_from` bound (column + key), plus the
    `valid_to` bound (column + key) when `with_valid_to`."""
    columns = [
        ColumnDecl(name="id", **{"from": "record_id"}),
        ColumnDecl(name="status", **{"from": "prop__status"}),
        ColumnDecl(
            name="valid_from",
            derived=DerivedSpec(
                scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
            ),
        ),
        ColumnDecl(
            name="valid_from_date_key", date_ref=DateRefSpec(scd_window="valid_from")
        ),
    ]
    if with_valid_to:
        columns.append(
            ColumnDecl(
                name="valid_to",
                derived=DerivedSpec(
                    scd_window=ScdWindowSpec(bound="valid_to", **{"as": "date"})
                ),
            )
        )
        columns.append(
            ColumnDecl(
                name="valid_to_date_key", date_ref=DateRefSpec(scd_window="valid_to")
            )
        )
    return TableDecl(
        name="dim_company",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="company"),
        key=["id", "valid_from"],
        columns=columns,
    )


@pytest.mark.parametrize("zone", ["UTC", "America/New_York"])
def test_scd_window_bound_keys_agree_with_sibling_on_every_version(
    tmp_path: Path, zone: str
) -> None:
    """Every version row's bound key equals `date_key_expr` over its sibling
    `derived: scd_window {..., as: date}` column, under UTC and under a
    zone whose local date differs from the UTC date for one version."""
    emit_dir = _build_scd_window_emit(tmp_path)
    rows = _compile_and_run(
        emit_dir, _type2_dim_company_table_decl(with_valid_to=True), _anchor(zone)
    )
    for valid_from, valid_from_key, valid_to, valid_to_key in zip(
        rows["valid_from"],
        rows["valid_from_date_key"],
        rows["valid_to"],
        rows["valid_to_date_key"],
    ):
        assert valid_from_key == _date_key(valid_from)
        assert valid_to_key == (_date_key(valid_to) if valid_to is not None else None)


def test_scd_window_bound_key_crosses_local_midnight_under_new_york(
    tmp_path: Path,
) -> None:
    """c001's first version starts 2h after midnight UTC -- its `valid_from`
    key differs between UTC and America/New_York, though the family agrees
    under either zone."""
    emit_dir = _build_scd_window_emit(tmp_path)
    utc_rows = _compile_and_run(
        emit_dir, _type2_dim_company_table_decl(with_valid_to=True), _anchor("UTC")
    )
    ny_rows = _compile_and_run(
        emit_dir,
        _type2_dim_company_table_decl(with_valid_to=True),
        _anchor("America/New_York"),
    )

    def _c001_first_version_key(rows: dict[str, list[object]]) -> object:
        c001 = [
            (valid_from, key)
            for record_id, valid_from, key in zip(
                rows["id"], rows["valid_from"], rows["valid_from_date_key"]
            )
            if record_id == "c001"
        ]
        return min(c001, key=lambda pair: pair[0])[1]

    assert _c001_first_version_key(utc_rows) != _c001_first_version_key(ny_rows)


def test_scd_window_valid_to_key_null_on_open_version_valid_from_never_null(
    tmp_path: Path,
) -> None:
    """`valid_to_date_key` is NULL exactly on each record's open version;
    `valid_from_date_key` is never NULL across any row."""
    emit_dir = _build_scd_window_emit(tmp_path)
    rows = _compile_and_run(
        emit_dir, _type2_dim_company_table_decl(with_valid_to=True), _UTC
    )
    assert all(key is not None for key in rows["valid_from_date_key"])
    for valid_to, valid_to_key in zip(rows["valid_to"], rows["valid_to_date_key"]):
        assert (valid_to is None) == (valid_to_key is None)
    open_ids = {
        record_id
        for record_id, valid_to in zip(rows["id"], rows["valid_to"])
        if valid_to is None
    }
    assert open_ids == {"c001", "c002"}


def test_scd_window_same_local_date_versions_share_valid_from_key(
    tmp_path: Path,
) -> None:
    """c001's first two versions land on the same America/New_York local
    date and share one `valid_from` key; both rows survive (3 total, not
    deduplicated) and the third (later) version carries a distinct key."""
    emit_dir = _build_scd_window_emit(tmp_path)
    rows = _compile_and_run(
        emit_dir,
        _type2_dim_company_table_decl(with_valid_to=True),
        _anchor("America/New_York"),
    )
    c001_keys = sorted(
        key
        for record_id, key in zip(rows["id"], rows["valid_from_date_key"])
        if record_id == "c001"
    )
    assert len(c001_keys) == 3
    assert c001_keys[0] == c001_keys[1]
    assert c001_keys[2] != c001_keys[1]


def test_scd_window_bound_key_without_sibling_compiles_and_executes(
    tmp_path: Path,
) -> None:
    """The `valid_to` bound key needn't travel beside a `derived: scd_window
    {valid_to, ...}` sibling column -- the date needn't be output alongside
    the key (`valid_from` alone satisfies Scd2NeedsHistory)."""
    emit_dir = _build_scd_window_emit(tmp_path)
    table_decl = TableDecl(
        name="dim_company",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="company"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(
                name="valid_from",
                derived=DerivedSpec(
                    scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
                ),
            ),
            ColumnDecl(
                name="valid_to_date_key", date_ref=DateRefSpec(scd_window="valid_to")
            ),
        ],
    )
    rows = _compile_and_run(emit_dir, table_decl, _UTC)
    assert "valid_to" not in rows
    open_ids = {
        record_id
        for record_id, key in zip(rows["id"], rows["valid_to_date_key"])
        if key is None
    }
    assert open_ids == {"c001", "c002"}


def test_build_column_expr_bound_shape_date_ref_raises_assertion() -> None:
    """The records-grain builder never sees the bound shape in practice --
    `DateRefWindowBoundRequiresScd2` refuses it on every non-type-2 table
    before compile -- so calling it directly asserts, naming the
    source-column requirement."""
    col_decl = ColumnDecl(
        name="valid_from_date_key", date_ref=DateRefSpec(scd_window="valid_from")
    )
    table_decl = TableDecl(
        name="dim_company",
        role="dim",
        source=SourceDecl(grain="records", kind="company"),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"}), col_decl],
    )
    with pytest.raises(
        AssertionError, match="date_ref on the records-grain builder reads"
    ):
        build_column_expr(col_decl, _UTC, table_decl=table_decl)


def test_build_query_specs_scd_window_bound_sql_deterministic(tmp_path: Path) -> None:
    """Two compiles of the same bound-shape config produce byte-identical
    SQL."""
    emit_dir = _build_scd_window_emit(tmp_path)
    config = DimensionalConfig(
        tables=[_type2_dim_company_table_decl(with_valid_to=True)]
    )
    with open_emit(emit_dir) as emit:
        first = build_query_specs(
            emit,
            config,
            _UTC,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            tables=None,
        )
        second = build_query_specs(
            emit,
            config,
            _UTC,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            tables=None,
        )

    assert [s.sql for s in first] == [s.sql for s in second]


# ---------------------------------------------------------------------------
# Determinism: byte-identical SQL across two compiles
# ---------------------------------------------------------------------------


def test_build_query_specs_date_ref_sql_deterministic(tmp_path: Path) -> None:
    """Two compiles of the same `date_ref`-bearing config produce
    byte-identical SQL."""
    emit_dir = _build_date_ref_emit(tmp_path)
    config = DimensionalConfig(tables=[_fact_created_date_table_decl()])
    with open_emit(emit_dir) as emit:
        first = build_query_specs(
            emit,
            config,
            _UTC,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            tables=None,
        )
        second = build_query_specs(
            emit,
            config,
            _UTC,
            None,
            notice_sink=discard_notice_sink,
            base_relations=None,
            tables=None,
        )

    assert [s.sql for s in first] == [s.sql for s in second]
