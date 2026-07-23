"""Tests for build_base_query_specs and export_base.

Full-export cases (`window=None`, no `slice_at`) pass every spec
write_mode='create' with no companion view. `slice_at` cases confirm the
horizon threaded to the render is `slice_at + 1`; windowed cases confirm the
horizon is `window.end_ns` and every spec is write_mode='replace'. Multi-kind
cases confirm deterministic sidecar-declaration order and that a 0-row kind
is still compiled and written, never dropped.
"""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb
from _support.notices import discard_notice_sink

from fabulexa_forge.config.models import BaseConfig, ExportConfig
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.engine import build_base_query_specs, export_base
from fabulexa_forge.exporters.base.plan import build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.query_spec import QuerySpec
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit

from ._base_fixtures import DAY_NS, build_base_test_emit, build_multi_kind_base_emit

_BASE_CONFIG = ExportConfig(mode="base")


# ---------------------------------------------------------------------------
# Full export (window=None, no slice_at)
# ---------------------------------------------------------------------------


def test_build_base_query_specs_full_export_write_mode(tmp_path: Path) -> None:
    """Every full-export spec is write_mode='create' with no companion view."""
    emit_dir = build_base_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(
            emit, _BASE_CONFIG, None, None, notice_sink=discard_notice_sink
        )

    assert specs
    for spec in specs:
        assert isinstance(spec, QuerySpec)
        assert spec.write_mode == "create"
        assert spec.view_name is None
        assert spec.view_sql is None


def test_export_base_anchor_none_succeeds(tmp_path: Path) -> None:
    """export_base with anchor=None succeeds — base has no anchor-required gate."""
    emit_dir = build_base_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        row_counts = export_base(
            emit,
            _BASE_CONFIG,
            out_path,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
        )

    assert row_counts == {"patient": 3}


# ---------------------------------------------------------------------------
# slice_at: horizon = slice_at + 1
# ---------------------------------------------------------------------------


def test_build_base_query_specs_slice_at_horizon_is_slice_at_plus_one(
    tmp_path: Path,
) -> None:
    """slice_at: T with window=None renders at horizon T + 1."""
    emit_dir = build_base_test_emit(tmp_path)
    config = ExportConfig(mode="base", base=BaseConfig(slice_at=2 * DAY_NS))
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(
            emit, config, None, None, notice_sink=discard_notice_sink
        )

        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(
            emit.sidecar, config.base, notice_sink=discard_notice_sink
        )
        spec = next(t for t in plan.tables if t.kind == "patient")
        expected_sql = build_base_render_sql(
            emit.sidecar, fork_path, spec, None, 2 * DAY_NS + 1
        )

    by_table = {s.table_name: s for s in specs}
    assert by_table["patient"].sql == expected_sql
    assert by_table["patient"].write_mode == "create"


# ---------------------------------------------------------------------------
# window set: horizon = window.end_ns, write_mode='replace'
# ---------------------------------------------------------------------------


def test_build_base_query_specs_windowed_horizon_and_write_mode(tmp_path: Path) -> None:
    """A windowed compile renders at window.end_ns and tags every spec 'replace'."""
    emit_dir = build_base_test_emit(tmp_path)
    window = Window(index=0, start_ns=0, end_ns=3 * DAY_NS, label="w0")
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(
            emit, _BASE_CONFIG, None, window, notice_sink=discard_notice_sink
        )

        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "patient")
        expected_sql = build_base_render_sql(
            emit.sidecar, fork_path, spec, None, 3 * DAY_NS
        )

    assert len(specs) == 1
    assert specs[0].sql == expected_sql
    assert specs[0].write_mode == "replace"


# ---------------------------------------------------------------------------
# Multi-kind: deterministic order, 0-row kind still emitted
# ---------------------------------------------------------------------------


def test_build_base_query_specs_deterministic_sidecar_order(tmp_path: Path) -> None:
    """One QuerySpec per surviving kind, in sidecar table-declaration order."""
    emit_dir = build_multi_kind_base_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(
            emit, _BASE_CONFIG, None, None, notice_sink=discard_notice_sink
        )

    assert [spec.table_name for spec in specs] == ["patient", "doctor"]


def test_export_base_zero_row_kind_still_emitted(tmp_path: Path) -> None:
    """A kind whose table materializes no rows is still emitted, never dropped."""
    emit_dir = build_multi_kind_base_emit(tmp_path)

    duckdb_out = tmp_path / "out.duckdb"
    csv_out = tmp_path / "csv_out"
    csv_out.mkdir()
    with open_emit(emit_dir) as emit:
        duckdb_counts = export_base(
            emit,
            _BASE_CONFIG,
            duckdb_out,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
        )
        csv_counts = export_base(
            emit, _BASE_CONFIG, csv_out, "csv", None, notice_sink=discard_notice_sink
        )

    assert duckdb_counts == {"patient": 1, "doctor": 0}
    assert csv_counts == {"patient": 1, "doctor": 0}

    out_conn = duckdb.connect(str(duckdb_out), read_only=True)
    try:
        assert out_conn.execute('SELECT COUNT(*) FROM "doctor"').fetchone() == (0,)
    finally:
        out_conn.close()

    doctor_csv = csv_out / "doctor.csv"
    assert doctor_csv.exists()
    with doctor_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 1  # header row only


# ---------------------------------------------------------------------------
# export_base: duckdb / csv full-format dispatch
# ---------------------------------------------------------------------------


def test_export_base_duckdb_writes_one_table_per_kind(tmp_path: Path) -> None:
    """export_base(fmt='duckdb') writes one table per kind and returns row counts."""
    emit_dir = build_base_test_emit(tmp_path)
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        row_counts = export_base(
            emit,
            _BASE_CONFIG,
            out_path,
            "duckdb",
            None,
            notice_sink=discard_notice_sink,
        )

    assert row_counts == {"patient": 3}
    out_conn = duckdb.connect(str(out_path), read_only=True)
    try:
        actual = out_conn.execute('SELECT COUNT(*) FROM "patient"').fetchone()
        assert actual is not None
        assert actual[0] == 3
    finally:
        out_conn.close()


def test_export_base_csv_writes_one_file_per_kind(tmp_path: Path) -> None:
    """export_base(fmt='csv') writes one <table>.csv per output table."""
    emit_dir = build_base_test_emit(tmp_path)
    out_dir = tmp_path / "csv_out"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        row_counts = export_base(
            emit, _BASE_CONFIG, out_dir, "csv", None, notice_sink=discard_notice_sink
        )

    assert row_counts == {"patient": 3}
    csv_path = out_dir / "patient.csv"
    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as fh:
        data_rows = list(csv.reader(fh))[1:]  # drop the header row
    assert len(data_rows) == 3
