"""End-to-end tests: the generated calendar (`dim_date`) and the `date_ref`
range guard through `export_dimensional`, full export only.

Covers: table order (declared tables, then dim_date, then supplements), the
CSV and DuckDB writes of dim_date, the fact-to-calendar join, a
`date_dimension` block with no `date_ref` anywhere, the `dim_date` overlay
slot, the source-is-output gate over a supplement's `dim_date.csv` file, and
the range-guard refusal leaving no output behind.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateDimensionConfig,
    DateParseSpec,
    DateRefSpec,
    DerivedSpec,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    SupplementDecl,
    TableDecl,
)
from fabulexa_forge.errors import DateRefOutOfRange, SupplementValueInvalid
from fabulexa_forge.exporters.companion import companion_artifact_paths
from fabulexa_forge.exporters.companion.overlay import load_readme_overlay
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.reader.emit import open_emit

_ANCHOR = EffectiveAnchor(
    start_instant=datetime.fromisoformat("2024-01-01T00:00:00+00:00"),
    timezone=ZoneInfo("UTC"),
)

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


def _build_emit(tmp_path: Path, *, p002_dob: str = "1990-05-14") -> Path:
    """Two patients: p001 dob 1985-03-02 (always in range), p002 dob
    `p002_dob` (in range by default; the violation tests override it)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "1985-03-02"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 0, True, None, 0, 1, p002_dob],
    )
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
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _dim_patient_decl() -> TableDecl:
    return TableDecl(
        name="dim_patient",
        role="dim",
        scd="type1",
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


def _dim_actor_decl_no_date_ref() -> TableDecl:
    """A declared table with no `date_ref` anywhere."""
    return TableDecl(
        name="dim_patient_plain",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id"],
        columns=[ColumnDecl(name="id", **{"from": "record_id"})],
    )


def _config(
    table: TableDecl,
    *,
    from_: str = "1980-01-01",
    to: str = "2026-12-31",
    supplements: list[SupplementDecl] | None = None,
) -> ExportConfig:
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(tables=[table]),
        date_dimension=DateDimensionConfig.model_validate({"from": from_, "to": to}),
        supplements=supplements,
    )


# ---------------------------------------------------------------------------
# Bound-shape date_ref: full export + the shape-blind range guard
# ---------------------------------------------------------------------------

_DAY = 86_400_000_000_000

_PATIENT_SCD_COLUMNS: list[dict[str, object]] = [
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

_SCD_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_scd_emit(tmp_path: Path) -> Path:
    """One patient with a history-tracked `status`: "pending" at day 1
    (2024-01-02), "active" at day 2 (2024-01-03, open) -- two SCD-2
    versions."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_SCD_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", _DAY, True, _DAY, 0, "active"],
    )
    conn.execute(_create_ddl("history", _SCD_HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", _DAY, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", 2 * _DAY, "active"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__patient",
                "records",
                _PATIENT_SCD_COLUMNS,
                1,
                record_kind="patient",
            ),
            _table_spec("history", "fixed", _SCD_HISTORY_COLUMNS, 2),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 3 * _DAY}],
    )
    return tmp_path


def _dim_patient_scd_decl() -> TableDecl:
    """A type-2 dim carrying both SCD-2 bound keys beside their raw
    `scd_window` siblings."""
    return TableDecl(
        name="dim_patient_scd",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="patient"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            ColumnDecl(name="valid_to", derived=DerivedSpec(scd_window="valid_to")),
            ColumnDecl(
                name="valid_from_key", date_ref=DateRefSpec(scd_window="valid_from")
            ),
            ColumnDecl(
                name="valid_to_key", date_ref=DateRefSpec(scd_window="valid_to")
            ),
        ],
    )


@pytest.mark.parametrize("fmt", ["csv", "duckdb"])
def test_bound_key_dim_full_export_reports_window_bounds(
    tmp_path: Path, fmt: "Literal['csv', 'duckdb']"
) -> None:
    """A full export of a type-2 dim with both bound keys succeeds, writes
    the README and manifest, and reports `window_bounds` on the dim's
    report and `{}` on `dim_date`."""
    emit_dir = _build_scd_emit(tmp_path / "emit")
    config = _config(_dim_patient_scd_decl())
    out = tmp_path / ("out_csv" if fmt == "csv" else "out.duckdb")
    if fmt == "csv":
        out.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit, config, out, fmt, _ANCHOR, discard_notice_sink, None, ()
        )

    by_name = {table.name: table for table in report.tables}
    assert by_name["dim_patient_scd"].window_bounds == {
        "valid_from_key": "valid_from",
        "valid_to_key": "valid_to",
    }
    assert by_name["dim_date"].window_bounds == {}
    readme_path, manifest_path = companion_artifact_paths(out, "dimensional", fmt)
    assert readme_path.exists()
    assert manifest_path.exists()


def test_bound_key_out_of_range_refuses_before_any_write(tmp_path: Path) -> None:
    """A bound-shape key value outside the declared range refuses before any
    write -- the range guard is shape-blind (`check_date_refs_in_range`
    probes by `references`, not shape)."""
    emit_dir = _build_scd_emit(tmp_path / "emit")
    config = _config(_dim_patient_scd_decl(), from_="1980-01-01", to="2024-01-02")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange, match="20240103"):
            export_dimensional(
                emit, config, out_dir, "csv", _ANCHOR, discard_notice_sink, None, ()
            )

    assert list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Table order + writes
# ---------------------------------------------------------------------------


def test_csv_report_lists_declared_tables_then_dim_date(tmp_path: Path) -> None:
    """CSV: dim_date lands right after the declared tables and its file
    is written."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _config(_dim_patient_decl())
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, ()
        )

    assert [t.name for t in report.tables] == ["dim_patient", "dim_date"]
    assert (out_dir / "dim_date.csv").exists()


def test_duckdb_report_lists_declared_tables_then_dim_date(tmp_path: Path) -> None:
    """DuckDB: same table order; a dim_date table exists in the output."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _config(_dim_patient_decl())
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit, config, out_path, "duckdb", None, discard_notice_sink, None, ()
        )

    assert [t.name for t in report.tables] == ["dim_patient", "dim_date"]
    conn = duckdb.connect(str(out_path), read_only=True)
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    join_count = conn.execute(
        'SELECT COUNT(*) FROM "dim_patient" p JOIN "dim_date" d'
        ' ON p."dob_key" = d."date_key" WHERE p."dob_key" IS NOT NULL'
    ).fetchone()
    non_null_count = conn.execute(
        'SELECT COUNT(*) FROM "dim_patient" WHERE "dob_key" IS NOT NULL'
    ).fetchone()
    conn.close()
    assert "dim_date" in tables
    assert join_count == non_null_count


def test_no_date_ref_anywhere_still_writes_dim_date(tmp_path: Path) -> None:
    """A `date_dimension` block with no `date_ref` column anywhere still
    materializes dim_date."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _config(_dim_actor_decl_no_date_ref())
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, ()
        )

    assert [t.name for t in report.tables] == ["dim_patient_plain", "dim_date"]
    assert (out_dir / "dim_date.csv").exists()


# ---------------------------------------------------------------------------
# Overlay slot
# ---------------------------------------------------------------------------


def test_overlay_table_slot_naming_dim_date_is_valid(tmp_path: Path) -> None:
    """A `## table: dim_date` overlay slot validates and its note renders."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _config(_dim_patient_decl())
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    overlay_path = tmp_path / "overlay.md"
    overlay_path.write_text(
        "## table: dim_date\n\nAuthor note on dim_date.\n", encoding="utf-8"
    )
    overlay = load_readme_overlay(overlay_path)

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, overlay, ()
        )

    readme_path, _manifest_path = companion_artifact_paths(
        out_dir, "dimensional", "csv"
    )
    assert "Author note on dim_date." in readme_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Supplement source-is-output over dim_date.csv
# ---------------------------------------------------------------------------


def test_supplement_file_resolving_to_dim_date_csv_is_refused(tmp_path: Path) -> None:
    """A file supplement resolving to the output dir's dim_date.csv is
    refused by the source-is-output gate."""
    emit_dir = _build_emit(tmp_path / "emit")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "dim_date.csv").write_text("x\n1\n", encoding="utf-8")
    decl = SupplementDecl(
        name="a_supplement", file="dim_date.csv", columns={"x": "VARCHAR"}
    )
    config = _config(_dim_patient_decl(), supplements=[decl])
    supplements = load_supplements(config, out_dir)

    with open_emit(emit_dir) as emit:
        with pytest.raises(Exception, match="dim_date.csv"):
            export_dimensional(
                emit,
                config,
                out_dir,
                "csv",
                None,
                discard_notice_sink,
                None,
                supplements,
            )


# ---------------------------------------------------------------------------
# The range guard
# ---------------------------------------------------------------------------


def test_violating_key_refuses_before_any_write(tmp_path: Path) -> None:
    """A dob outside the declared range refuses before any write; the
    output directory holds no data file and no DuckDB file afterward."""
    emit_dir = _build_emit(tmp_path / "emit", p002_dob="2030-01-01")
    config = _config(_dim_patient_decl(), from_="1980-01-01", to="2026-12-31")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange, match="20300101"):
            export_dimensional(
                emit, config, out_dir, "csv", None, discard_notice_sink, None, ()
            )

    assert list(out_dir.iterdir()) == []


def test_violating_key_refuses_before_any_duckdb_write(tmp_path: Path) -> None:
    """Same refusal under fmt='duckdb': no .duckdb file is created."""
    emit_dir = _build_emit(tmp_path / "emit", p002_dob="2030-01-01")
    config = _config(_dim_patient_decl(), from_="1980-01-01", to="2026-12-31")
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        with pytest.raises(DateRefOutOfRange):
            export_dimensional(
                emit, config, out_path, "duckdb", None, discard_notice_sink, None, ()
            )

    assert not out_path.exists()


def test_guard_runs_after_the_supplement_cell_probe(tmp_path: Path) -> None:
    """A bad supplement cell is reported, not the range violation."""
    emit_dir = _build_emit(tmp_path / "emit", p002_dob="2030-01-01")
    decl = SupplementDecl(
        name="bad_supplement",
        columns={"n": "BIGINT"},
        rows=[{"n": "not-a-number"}],
    )
    config = _config(
        _dim_patient_decl(), from_="1980-01-01", to="2026-12-31", supplements=[decl]
    )
    supplements = load_supplements(config, tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementValueInvalid):
            export_dimensional(
                emit,
                config,
                out_dir,
                "csv",
                None,
                discard_notice_sink,
                None,
                supplements,
            )

    assert list(out_dir.iterdir()) == []
