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

from exporters.source.test_where_plan import _write_sensor_emit
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
    build_windowed_source_query_specs,
    export_source,
    require_source_anchor,
)
from fabulexa_forge.exporters.source.plan import SourcePlan, build_source_plan
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import (
    build_empty_source_emit,
    build_source_keys_emit,
    build_source_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from fabulexa_forge.exporters.notices import NoticeSink
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
) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Open `emit_dir` and build a SourcePlan, resolving the anchor and
    election the way `export_source` does."""
    config = _config(tables, events=events, declare_keys=declare_keys)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(emit, config, anchor, election, discard_notice_sink)
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
        specs = build_source_query_specs(plan)

    assert specs
    for spec in specs:
        assert isinstance(spec, QuerySpec)
        assert spec.write_mode == "create"
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
        specs = build_source_query_specs(plan)
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
        specs_a = build_source_query_specs(plan)
        specs_b = build_source_query_specs(plan)
    assert [(s.table_name, s.sql, s.write_mode) for s in specs_a] == [
        (s.table_name, s.sql, s.write_mode) for s in specs_b
    ]


# ---------------------------------------------------------------------------
# build_windowed_source_query_specs: tables=None, selection, notices, the
# undeclared-name assertion.
# ---------------------------------------------------------------------------


def _windowed_specs(
    emit_dir: Path,
    config: ExportConfig,
    window: Window,
    *,
    notice_sink: "NoticeSink" = discard_notice_sink,
    tables: "Collection[str] | None",
) -> list[QuerySpec]:
    """Open `emit_dir` and run `build_windowed_source_query_specs` the way
    the shaped seam does: resolve the anchor and election, then the horizon
    compile, optionally a selection."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        return build_windowed_source_query_specs(
            emit, config, anchor, election, window, notice_sink, tables=tables
        )


def test_windowed_query_specs_tables_none_returns_every_unit_event_log_last(
    tmp_path: Path,
) -> None:
    events = SourceEventsDecl(
        name="visit_versions", sources=(SourceEventSourceDecl(kind="visit"),)
    )
    config = _config(_WINDOWED_TABLES, events=events)
    window = windowed_test_windows()[0]
    specs = _windowed_specs(
        build_windowed_source_test_emit(tmp_path), config, window, tables=None
    )
    assert [s.table_name for s in specs] == [
        "visit",
        "order",
        "location",
        "visit_team",
        "visit_versions",
    ]
    assert specs[-1].write_mode == "append"


_SENSOR_STATE_TABLE = SourceTableDecl(
    name="sensor_state", kind="sensor", where={"prop__category": "underground"}
)
_SENSOR_EVENTS = SourceEventsDecl(
    name="sensor_versions", sources=(SourceEventSourceDecl(kind="sensor"),)
)
_SENSOR_WINDOW = Window(index=None, start_ns=0, end_ns=100, label="")


def test_windowed_query_specs_state_only_selection_replace_notice_once(
    tmp_path: Path,
) -> None:
    """A state-table-only selection returns that unit's spec (write_mode
    'replace') and emits its plan notice once (only the end horizon opens)."""
    config = _config((_SENSOR_STATE_TABLE,), events=_SENSOR_EVENTS)
    sink = RecordingNoticeSink()
    specs = _windowed_specs(
        _write_sensor_emit(tmp_path),
        config,
        _SENSOR_WINDOW,
        notice_sink=sink,
        tables={"sensor_state"},
    )
    assert [s.table_name for s in specs] == ["sensor_state"]
    assert specs[0].write_mode == "replace"
    assert len(sink.notices) == 1


def test_windowed_query_specs_event_log_selection_append_last_notice_twice(
    tmp_path: Path,
) -> None:
    """A selection including the event log returns it last with write_mode
    'append' and emits each plan notice twice (both horizons open)."""
    config = _config((_SENSOR_STATE_TABLE,), events=_SENSOR_EVENTS)
    sink = RecordingNoticeSink()
    specs = _windowed_specs(
        _write_sensor_emit(tmp_path),
        config,
        _SENSOR_WINDOW,
        notice_sink=sink,
        tables={"sensor_state", "sensor_versions"},
    )
    assert [s.table_name for s in specs] == ["sensor_state", "sensor_versions"]
    assert specs[-1].write_mode == "append"
    assert len(sink.notices) == 2


def test_windowed_query_specs_selected_spec_equals_tables_none_spec(
    tmp_path: Path,
) -> None:
    """The selected spec equals the same unit's spec under tables=None."""
    config = _config((_SENSOR_STATE_TABLE,))
    emit_dir = _write_sensor_emit(tmp_path)
    whole = _windowed_specs(emit_dir, config, _SENSOR_WINDOW, tables=None)
    selected = _windowed_specs(
        emit_dir, config, _SENSOR_WINDOW, tables={"sensor_state"}
    )
    assert len(whole) == 1
    assert len(selected) == 1
    assert selected[0].sql == whole[0].sql
    assert selected[0].write_mode == whole[0].write_mode


def test_windowed_query_specs_unknown_name_asserts(tmp_path: Path) -> None:
    config = _config((_SENSOR_STATE_TABLE,))
    with pytest.raises(AssertionError):
        _windowed_specs(
            _write_sensor_emit(tmp_path),
            config,
            _SENSOR_WINDOW,
            tables={"nonexistent_table"},
        )


# ---------------------------------------------------------------------------
# build_source_query_specs: declare_keys
# ---------------------------------------------------------------------------


def test_build_source_query_specs_declare_keys_absent_all_unkeyed(
    tmp_path: Path,
) -> None:
    """declare_keys absent -> every spec's keys is None."""
    with _plan(build_source_keys_emit(tmp_path), _KEYS_TABLES) as (emit, plan):
        specs = build_source_query_specs(plan)
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
        specs = build_source_query_specs(plan)
    by_table = {spec.table_name: spec for spec in specs}
    assert by_table["visit"].keys is not None
    assert by_table["visit"].keys.unique == (("presentation_id",),)
    assert by_table["consultant"].keys is not None
    assert by_table["consultant"].keys.unique == (("presentation_id",),)
    assert by_table["nurse"].keys is not None
    assert by_table["nurse"].keys.unique == ()
    assert by_table["visit_team"].keys is None


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
        plan = build_source_plan(emit, config, anchor, election, discard_notice_sink)
        specs_a = build_source_query_specs(plan)
        specs_b = build_source_query_specs(plan)
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
