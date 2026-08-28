"""Tests for `require_source_anchor`, `build_source_query_specs`, and
`export_source` (`exporters/source/engine.py`).

Full-export cases (`window=None`) pass every spec `write_mode='create'`.
Windowed cases tag per-unit write_mode: `state` `replace` (a full horizon
snapshot per window), `junction` / the event log `append` (extract-on-change
/ append-only). Compile order mirrors `plan.tables` declaration order, the
event log last. `build_source_query_specs` raises `ValueError` when `window`
presence disagrees with the plan's own `windowed` flag — a caller
programming error, guarded here as a contract check.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import duckdb
import pytest
from _support.duckdb_introspect import constraint_types
from _support.notices import RecordingNoticeSink, discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import SourceAnchorRequired
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import (
    NOTICE_KEYS_NOT_DECLARABLE_CSV,
    QuerySpec,
)
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
    require_source_anchor,
)
from fabulexa_forge.exporters.source.plan import SourcePlan, build_source_plan
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import (
    build_empty_source_emit,
    build_source_keys_emit,
    build_source_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Config / plan-build helpers
# ---------------------------------------------------------------------------

_SPANNING_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="visit", kind="visit"),
    SourceTableDecl(name="shift", kind="shift"),
    SourceTableDecl(name="location", kind="location"),
    SourceTableDecl(name="order", kind="order"),
    SourceTableDecl(name="consultant", kind="actor", sub_types=("consultant",)),
    SourceTableDecl(name="nurse", kind="actor", sub_types=("nurse",)),
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)

_WINDOWED_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="visit", kind="visit"),
    SourceTableDecl(name="order", kind="order"),
    SourceTableDecl(name="location", kind="location"),
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)

_KEYS_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="visit", kind="visit"),
    SourceTableDecl(name="consultant", kind="actor", sub_types=("consultant",)),
    SourceTableDecl(name="nurse", kind="actor", sub_types=("nurse",)),
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)

_EXPECTED_ROW_COUNTS = {
    "visit": 3,  # one row per record, not one per event
    "shift": 1,
    "location": 2,
    "order": 1,
    "consultant": 1,
    "nurse": 1,
    "visit_team": 2,
}


def _config(
    tables: "tuple[SourceTableDecl, ...]",
    *,
    events: "SourceEventsDecl | None" = None,
    declare_keys: bool = False,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(tables=tables, events=events, declare_keys=declare_keys),
    )


@contextmanager
def _plan(
    emit_dir: Path,
    tables: "tuple[SourceTableDecl, ...]",
    *,
    events: "SourceEventsDecl | None" = None,
    declare_keys: bool = False,
    windowed: bool = False,
) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Open `emit_dir` and build a SourcePlan, resolving the anchor and
    election the way `export_source` does."""
    config = _config(tables, events=events, declare_keys=declare_keys)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, windowed, discard_notice_sink
        )
        yield emit, plan


def _unique_constraint_columns(out_path: Path, table_name: str) -> list[list[str]]:
    """The column lists of every declared UNIQUE constraint on a table
    (excludes the PRIMARY KEY's own implicit UNIQUE row)."""
    conn = duckdb.connect(str(out_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints()"
            " WHERE table_name = ? AND constraint_type = 'UNIQUE'",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    return [list(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# require_source_anchor
# ---------------------------------------------------------------------------


def test_require_source_anchor_raises_on_none() -> None:
    """A None anchor resolution raises SourceAnchorRequired."""
    with pytest.raises(SourceAnchorRequired):
        require_source_anchor(None)


def test_require_source_anchor_returns_narrowed_anchor(tmp_path: Path) -> None:
    """A resolved anchor passes through unchanged."""
    with open_emit(build_source_test_emit(tmp_path)) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    assert anchor is not None
    assert require_source_anchor(anchor) is anchor


# ---------------------------------------------------------------------------
# build_source_query_specs: full export
# ---------------------------------------------------------------------------


def test_build_source_query_specs_full_export_write_mode(tmp_path: Path) -> None:
    """Every full-export spec is write_mode='create' with no companion view."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        specs = build_source_query_specs(plan, None)

    assert specs
    for spec in specs:
        assert isinstance(spec, QuerySpec)
        assert spec.write_mode == "create"
        assert spec.view_name is None
        assert spec.view_sql is None
    assert {spec.table_name for spec in specs} == set(_EXPECTED_ROW_COUNTS)


def test_build_source_query_specs_compile_order_event_log_last(
    tmp_path: Path,
) -> None:
    """`plan.tables` declaration order is preserved; the event log compiles last."""
    tables = (
        SourceTableDecl(name="shift", kind="shift"),
        SourceTableDecl(name="visit", kind="visit"),
        SourceTableDecl(name="location", kind="location"),
    )
    events = SourceEventsDecl(
        name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
    )
    with _plan(build_source_test_emit(tmp_path), tables, events=events) as (
        emit,
        plan,
    ):
        specs = build_source_query_specs(plan, None)
    assert [spec.table_name for spec in specs] == [
        "shift",
        "visit",
        "location",
        "versions",
    ]
    assert specs[-1].write_mode == "create"


def test_build_source_query_specs_determinism(tmp_path: Path) -> None:
    """Two compiles of the same plan produce identical (table, sql, mode) specs."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        specs_a = build_source_query_specs(plan, None)
        specs_b = build_source_query_specs(plan, None)
    assert [(s.table_name, s.sql, s.write_mode) for s in specs_a] == [
        (s.table_name, s.sql, s.write_mode) for s in specs_b
    ]


# ---------------------------------------------------------------------------
# build_source_query_specs: windowed compile
# ---------------------------------------------------------------------------


def test_build_source_query_specs_windowed_write_mode_per_unit(
    tmp_path: Path,
) -> None:
    """Windowed compile tags write_mode per unit kind: state replace,
    junction append; no unit uses a companion view."""
    window, _, _ = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        specs = build_source_query_specs(plan, window)

    write_mode_by_table = {spec.table_name: spec.write_mode for spec in specs}
    assert write_mode_by_table == {
        "visit": "replace",
        "order": "replace",
        "location": "replace",
        "visit_team": "append",
    }
    for spec in specs:
        assert spec.view_name is None
        assert spec.view_sql is None


def test_build_source_query_specs_windowed_event_log_appends(
    tmp_path: Path,
) -> None:
    """A windowed compile's event-log spec is write_mode='append'."""
    window, _, _ = windowed_test_windows()
    events = SourceEventsDecl(
        name="versions", sources=(SourceEventSourceDecl(kind="visit"),)
    )
    with _plan(
        build_windowed_source_test_emit(tmp_path),
        _WINDOWED_TABLES,
        events=events,
        windowed=True,
    ) as (emit, plan):
        specs = build_source_query_specs(plan, window)
    by_table = {spec.table_name: spec for spec in specs}
    assert by_table["versions"].write_mode == "append"


def test_build_source_query_specs_window_presence_mismatch_raises(
    tmp_path: Path,
) -> None:
    """`window` presence disagreeing with the plan's own windowed-ness raises."""
    window, _, _ = windowed_test_windows()
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    windowed_dir = tmp_path / "windowed"
    windowed_dir.mkdir()

    with _plan(build_source_test_emit(full_dir), _SPANNING_TABLES) as (emit, plan):
        with pytest.raises(ValueError, match="windowed-ness"):
            build_source_query_specs(plan, window)

    with _plan(
        build_windowed_source_test_emit(windowed_dir), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        with pytest.raises(ValueError, match="windowed-ness"):
            build_source_query_specs(plan, None)


# ---------------------------------------------------------------------------
# build_source_query_specs: declare_keys
# ---------------------------------------------------------------------------


def test_build_source_query_specs_declare_keys_absent_all_unkeyed(
    tmp_path: Path,
) -> None:
    """declare_keys absent -> every spec's keys is None."""
    with _plan(build_source_keys_emit(tmp_path), _KEYS_TABLES) as (emit, plan):
        specs = build_source_query_specs(plan, None)
    assert specs
    assert all(spec.keys is None for spec in specs)


def test_build_source_query_specs_declare_keys_per_table(tmp_path: Path) -> None:
    """declare_keys: true -> the claimed split unit ('consultant') carries a
    presentation_id UNIQUE, the unclaimed one ('nurse') identity keys only,
    and the junction table declares no keys at all."""
    with _plan(build_source_keys_emit(tmp_path), _KEYS_TABLES, declare_keys=True) as (
        emit,
        plan,
    ):
        specs = build_source_query_specs(plan, None)
    by_table = {spec.table_name: spec for spec in specs}
    assert by_table["visit"].keys is not None
    assert by_table["visit"].keys.unique == (("presentation_id",),)
    assert by_table["consultant"].keys is not None
    assert by_table["consultant"].keys.unique == (("presentation_id",),)
    assert by_table["nurse"].keys is not None
    assert by_table["nurse"].keys.unique == ()
    assert by_table["visit_team"].keys is None


def test_build_source_query_specs_declare_keys_windowed_matches_full(
    tmp_path: Path,
) -> None:
    """A windowed compile's declared keys equal the full-export declaration."""
    window, _, _ = windowed_test_windows()
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    windowed_dir = tmp_path / "windowed"
    windowed_dir.mkdir()

    with _plan(build_source_keys_emit(full_dir), _KEYS_TABLES, declare_keys=True) as (
        emit,
        plan,
    ):
        full_specs = build_source_query_specs(plan, None)
    with _plan(
        build_source_keys_emit(windowed_dir),
        _KEYS_TABLES,
        declare_keys=True,
        windowed=True,
    ) as (emit, plan):
        windowed_specs = build_source_query_specs(plan, window)
    full_keys = {s.table_name: s.keys for s in full_specs}
    windowed_keys = {s.table_name: s.keys for s in windowed_specs}
    assert full_keys == windowed_keys


# ---------------------------------------------------------------------------
# export_source
# ---------------------------------------------------------------------------


def test_export_source_anchor_required(tmp_path: Path) -> None:
    """export_source raises SourceAnchorRequired before writing anything."""
    emit_dir = build_source_test_emit(tmp_path, with_runtime=False)
    config = _config(_SPANNING_TABLES)
    with open_emit(emit_dir) as emit:
        with pytest.raises(SourceAnchorRequired):
            export_source(
                emit,
                config,
                tmp_path / "out.duckdb",
                "duckdb",
                None,
                notice_sink=discard_notice_sink,
                overlay=None,
            )


def test_export_source_duckdb_row_counts(tmp_path: Path) -> None:
    """export_source(fmt='duckdb') returns every table's row count and writes it."""
    emit_dir = build_source_test_emit(tmp_path)
    config = _config(_SPANNING_TABLES)
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        report = export_source(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    row_counts = {t.name: t.row_count for t in report.tables}
    assert row_counts == _EXPECTED_ROW_COUNTS

    out_conn = duckdb.connect(str(out_path), read_only=True)
    try:
        for table_name, expected in _EXPECTED_ROW_COUNTS.items():
            actual = out_conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
            assert actual is not None
            assert actual[0] == expected
    finally:
        out_conn.close()


def test_export_source_csv_writes_one_file_per_table(tmp_path: Path) -> None:
    """export_source(fmt='csv') writes one <table>.csv per output table."""
    emit_dir = build_source_test_emit(tmp_path)
    config = _config(_SPANNING_TABLES)
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        report = export_source(
            emit,
            config,
            out_dir,
            "csv",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    row_counts = {t.name: t.row_count for t in report.tables}
    assert row_counts == _EXPECTED_ROW_COUNTS
    for table_name, expected in _EXPECTED_ROW_COUNTS.items():
        csv_path = out_dir / f"{table_name}.csv"
        assert csv_path.exists()
        with csv_path.open(newline="", encoding="utf-8") as fh:
            data_rows = list(csv.reader(fh))[1:]  # drop the header row
        assert len(data_rows) == expected


def test_export_source_zero_row_table_still_emitted(tmp_path: Path) -> None:
    """A table whose query resolves to no rows is still emitted, never dropped."""
    emit_dir = build_empty_source_emit(tmp_path)
    config = _config((SourceTableDecl(name="location", kind="location"),))

    duckdb_out = tmp_path / "empty.duckdb"
    csv_out = tmp_path / "empty_csv"
    csv_out.mkdir()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        duckdb_report = export_source(
            emit,
            config,
            duckdb_out,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )
        csv_report = export_source(
            emit,
            config,
            csv_out,
            "csv",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    duckdb_counts = {t.name: t.row_count for t in duckdb_report.tables}
    csv_counts = {t.name: t.row_count for t in csv_report.tables}
    assert duckdb_counts == {"location": 0}
    assert csv_counts == {"location": 0}

    out_conn = duckdb.connect(str(duckdb_out), read_only=True)
    try:
        assert out_conn.execute('SELECT COUNT(*) FROM "location"').fetchone() == (0,)
    finally:
        out_conn.close()

    csv_path = csv_out / "location.csv"
    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 1  # header row only


def test_export_source_determinism(tmp_path: Path) -> None:
    """Two full exports of the same emit compile identical (table, sql, mode)
    specs."""
    emit_dir = build_source_test_emit(tmp_path)
    config = _config(_SPANNING_TABLES)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        specs_a = build_source_query_specs(plan, None)
        specs_b = build_source_query_specs(plan, None)
    assert [(s.table_name, s.sql, s.write_mode) for s in specs_a] == [
        (s.table_name, s.sql, s.write_mode) for s in specs_b
    ]


# ---------------------------------------------------------------------------
# export_source: declare_keys
# ---------------------------------------------------------------------------


def test_export_source_csv_declare_keys_emits_one_notice_before_data(
    tmp_path: Path,
) -> None:
    """export_source CSV + declare_keys -> exactly one keys-not-declarable-csv
    notice, and the data is written unaffected."""
    emit_dir = build_source_keys_emit(tmp_path)
    config = _config(_KEYS_TABLES, declare_keys=True)
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        report = export_source(
            emit, config, out_dir, "csv", anchor, notice_sink=sink, overlay=None
        )

    row_counts = {t.name: t.row_count for t in report.tables}
    assert row_counts["consultant"] == 1
    codes = [n.code for n in sink.notices]
    assert codes.count(NOTICE_KEYS_NOT_DECLARABLE_CSV) == 1


def test_export_source_duckdb_declare_keys_emits_no_csv_notice(
    tmp_path: Path,
) -> None:
    """export_source DuckDB + declare_keys -> no keys-not-declarable-csv notice."""
    emit_dir = build_source_keys_emit(tmp_path)
    config = _config(_KEYS_TABLES, declare_keys=True)
    out_path = tmp_path / "out.duckdb"
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        export_source(
            emit, config, out_path, "duckdb", anchor, notice_sink=sink, overlay=None
        )

    codes = [n.code for n in sink.notices]
    assert NOTICE_KEYS_NOT_DECLARABLE_CSV not in codes


def test_export_source_duckdb_declare_keys_carries_constraints(
    tmp_path: Path,
) -> None:
    """An end-to-end DuckDB export carries the resolved constraints: the
    claimed split unit's UNIQUE constraint names presentation_id, the
    unclaimed one declares no presentation_id UNIQUE."""
    emit_dir = build_source_keys_emit(tmp_path)
    config = _config(_KEYS_TABLES, declare_keys=True)
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        export_source(
            emit,
            config,
            out_path,
            "duckdb",
            anchor,
            notice_sink=discard_notice_sink,
            overlay=None,
        )

    assert "PRIMARY KEY" in constraint_types(out_path, "consultant")
    assert ["presentation_id"] in _unique_constraint_columns(out_path, "consultant")

    assert "PRIMARY KEY" in constraint_types(out_path, "nurse")
    assert ["presentation_id"] not in _unique_constraint_columns(out_path, "nurse")
