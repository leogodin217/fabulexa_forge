"""Tests for build_source_query_specs and export_source.

Full-export cases (`window=None`) pass every spec write_mode='create'. Windowed
cases (Unit 2) confirm per-genre write_mode tagging: change-log/transaction
append, reference replace, junction append. Snapshot-delivery cases (Unit 3)
confirm SourceSnapshotRequiresWindows on a full (non-windowed) export and
write_mode='replace' + build_snapshot_render_sql routing for a windowed
changelog-genre spec.
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
import pytest

from fabulexa_export.anchor import resolve_effective_anchor
from fabulexa_export.config.models import ExportConfig, SourceConfig
from fabulexa_export.derivations.guard import require_single_branch
from fabulexa_export.errors import SourceAnchorRequired, SourceSnapshotRequiresWindows
from fabulexa_export.exporters.query_spec import QuerySpec
from fabulexa_export.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_export.exporters.source.plan import build_source_plan
from fabulexa_export.exporters.source.renders import (
    build_records_render_sql,
    build_snapshot_render_sql,
)
from fabulexa_export.reader.emit import open_emit

from ._source_fixtures import (
    build_empty_source_emit,
    build_source_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)

_EXPECTED_ROW_COUNTS = {
    "visit": 5,  # v001 c; v002 c, u; v003 c, d
    "shift": 2,  # sh001 c, d
    "location": 2,
    "order": 1,
    "consultant": 1,
    "nurse": 1,
    "visit_team": 2,
}


def test_build_source_query_specs_anchor_required(tmp_path: Path) -> None:
    """anchor=None raises SourceAnchorRequired."""
    emit_dir = build_source_test_emit(tmp_path, with_runtime=False)
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is None
        with pytest.raises(SourceAnchorRequired):
            build_source_query_specs(emit, config, anchor, None)


def test_export_source_anchor_required(tmp_path: Path) -> None:
    """export_source raises SourceAnchorRequired before writing anything."""
    emit_dir = build_source_test_emit(tmp_path, with_runtime=False)
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        with pytest.raises(SourceAnchorRequired):
            export_source(emit, config, tmp_path / "out.duckdb", "duckdb", None)


def test_build_source_query_specs_full_export_write_mode(tmp_path: Path) -> None:
    """Every full-export spec is write_mode='create' with no companion view."""
    emit_dir = build_source_test_emit(tmp_path)
    config = ExportConfig(mode="source")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        specs = build_source_query_specs(emit, config, anchor, None)

    assert specs
    for spec in specs:
        assert isinstance(spec, QuerySpec)
        assert spec.write_mode == "create"
        assert spec.view_name is None
        assert spec.view_sql is None
    assert {spec.table_name for spec in specs} == set(_EXPECTED_ROW_COUNTS)


def test_export_source_duckdb_row_counts(tmp_path: Path) -> None:
    """export_source(fmt='duckdb') returns every table's row count and writes it."""
    emit_dir = build_source_test_emit(tmp_path)
    config = ExportConfig(mode="source")
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        row_counts = export_source(emit, config, out_path, "duckdb", anchor)

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
    config = ExportConfig(mode="source")
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        row_counts = export_source(emit, config, out_dir, "csv", anchor)

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
    config = ExportConfig(mode="source")

    duckdb_out = tmp_path / "empty.duckdb"
    csv_out = tmp_path / "empty_csv"
    csv_out.mkdir()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        duckdb_counts = export_source(emit, config, duckdb_out, "duckdb", anchor)
        csv_counts = export_source(emit, config, csv_out, "csv", anchor)

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


# ---------------------------------------------------------------------------
# Windowed compile (Unit 2): per-genre write_mode
# ---------------------------------------------------------------------------


def test_build_source_query_specs_windowed_write_mode_per_genre(tmp_path: Path) -> None:
    """Windowed compile tags write_mode per genre: changelog/transaction append,
    reference replace, junction append; no source genre uses a companion view.
    """
    emit_dir = build_windowed_source_test_emit(tmp_path)
    config = ExportConfig(mode="source")
    window, _, _ = windowed_test_windows()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        specs = build_source_query_specs(emit, config, anchor, window)

    write_mode_by_table = {spec.table_name: spec.write_mode for spec in specs}
    assert write_mode_by_table == {
        "visit": "append",  # changelog genre
        "order": "append",  # transaction genre
        "location": "replace",  # reference genre
        "visit_team": "append",  # junction genre
    }
    for spec in specs:
        assert spec.view_name is None
        assert spec.view_sql is None


# ---------------------------------------------------------------------------
# Snapshot delivery (change_delivery: snapshot, Unit 3)
# ---------------------------------------------------------------------------

_SNAPSHOT_SOURCE_CONFIG = ExportConfig(
    mode="source", source=SourceConfig(change_delivery="snapshot")
)


def test_build_source_query_specs_snapshot_full_export_raises(tmp_path: Path) -> None:
    """change_delivery: snapshot with window=None raises
    SourceSnapshotRequiresWindows."""
    emit_dir = build_source_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        with pytest.raises(SourceSnapshotRequiresWindows):
            build_source_query_specs(emit, _SNAPSHOT_SOURCE_CONFIG, anchor, None)


def test_export_source_snapshot_full_export_raises(tmp_path: Path) -> None:
    """export_source under change_delivery: snapshot always raises
    SourceSnapshotRequiresWindows (a full export never carries a window)."""
    emit_dir = build_source_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        with pytest.raises(SourceSnapshotRequiresWindows):
            export_source(
                emit, _SNAPSHOT_SOURCE_CONFIG, tmp_path / "out.duckdb", "duckdb", anchor
            )


def test_build_source_query_specs_windowed_snapshot_write_mode_and_render(
    tmp_path: Path,
) -> None:
    """Windowed snapshot delivery tags the changelog-genre spec
    write_mode='replace' and routes it to build_snapshot_render_sql; reference
    and transaction specs are unaffected (same render, same write_mode)."""
    emit_dir = build_windowed_source_test_emit(tmp_path)
    window, _, _ = windowed_test_windows()
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        specs = build_source_query_specs(emit, _SNAPSHOT_SOURCE_CONFIG, anchor, window)

        by_table = {spec.table_name: spec for spec in specs}
        assert by_table["visit"].write_mode == "replace"  # changelog, snapshot delivery
        assert by_table["order"].write_mode == "append"  # transaction, unaffected
        assert by_table["location"].write_mode == "replace"  # reference, unaffected

        fork_path = require_single_branch(emit.sidecar)
        table_specs = build_source_plan(emit.sidecar, _SNAPSHOT_SOURCE_CONFIG.source)
        visit_spec = next(s for s in table_specs if s.source_table == "records__visit")
        order_spec = next(s for s in table_specs if s.source_table == "records__order")
        expected_visit_sql = build_snapshot_render_sql(
            emit.sidecar, fork_path, visit_spec, anchor, window
        )
        expected_order_sql = build_records_render_sql(
            emit.sidecar, fork_path, order_spec, anchor, window
        )

    assert by_table["visit"].sql == expected_visit_sql
    assert by_table["order"].sql == expected_order_sql
