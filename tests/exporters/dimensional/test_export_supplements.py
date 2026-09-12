"""End-to-end tests: supplements through export_dimensional, full export only.

Covers: CSV + DuckDB full export table order and TableReport.supplement
stamping, written-value round-trip (NULL / DATE / DECIMAL), CSV idempotency,
manifest supplement provenance + embedded config, the README's supplement
section and overlay slot, SupplementSourceIsOutput under CSV / DuckDB /
manifest-path collisions, a SupplementValueInvalid refusal leaving the output
empty, and a no-supplements export unaffected.
"""

from __future__ import annotations

import csv as csv_module
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, write_emit

from exporters._emit_fixtures import _create_ddl, _table_spec
from fabulexa_forge.config.models import (
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceDecl,
    SupplementDecl,
    TableDecl,
)
from fabulexa_forge.errors import (
    ReadmeOverlayUnknownTable,
    SupplementSourceIsOutput,
    SupplementValueInvalid,
)
from fabulexa_forge.exporters.companion import companion_artifact_paths
from fabulexa_forge.exporters.companion.overlay import load_readme_overlay
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.supplements import SupplementSource, load_supplements
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# Emit + config fixtures
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
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
]

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,\n"
_REGION_CODE_SHA256 = hashlib.sha256(_REGION_CODE_CSV.encode("utf-8")).hexdigest()


def _build_emit(tmp_path: Path) -> Path:
    """Minimal self-contained emit: one records__actor kind, two rows."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, None, 0, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", 0, True, None, 0, 1, "Bob"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 2, record_kind="actor"
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return tmp_path


def _dim_actor_decl() -> TableDecl:
    return TableDecl(
        name="dim_actor",
        role="dim",
        scd="type1",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="name", **{"from": "prop__name"}),
        ],
    )


def _region_code_decl(file: str = "region_code.csv") -> SupplementDecl:
    return SupplementDecl(
        name="region_code",
        file=file,
        columns={"code": "VARCHAR", "label": "VARCHAR"},
        description="Sales-region codes used by account management.",
        descriptions={"code": "ISO region code."},
    )


def _rate_tier_decl() -> SupplementDecl:
    return SupplementDecl(
        name="rate_tier",
        columns={
            "tier": "VARCHAR",
            "capacity": "BIGINT",
            "rate": "DECIMAL(5,2)",
            "effective_date": "DATE",
            "active": "BOOLEAN",
            "weight": "DOUBLE",
        },
        rows=[
            {
                "tier": "standard",
                "capacity": 100,
                "rate": 1.5,
                "effective_date": "2024-01-01",
                "active": "true",
                "weight": 0.5,
            },
            {
                "tier": "premium",
                "capacity": None,
                "rate": 2.25,
                "effective_date": "2024-06-01",
                "active": "false",
                "weight": 1.75,
            },
        ],
        description="Rate tiers offered to accounts.",
        descriptions={"tier": "Tier name."},
    )


def _make_config(supplements: list[SupplementDecl] | None = None) -> ExportConfig:
    return ExportConfig(
        mode="dimensional",
        dimensional=DimensionalConfig(tables=[_dim_actor_decl()]),
        supplements=supplements,
    )


def _happy_config_and_supplements(
    tmp_path: Path,
) -> tuple[ExportConfig, Path, tuple[object, ...]]:
    """A config declaring dim_actor + both supplements, resolved against a
    fresh config directory holding region_code.csv."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    config = _make_config([_region_code_decl(), _rate_tier_decl()])
    supplements = load_supplements(config, config_dir)
    return config, config_dir, supplements


# ---------------------------------------------------------------------------
# Full export: table order, TableReport.supplement, row counts
# ---------------------------------------------------------------------------


def test_csv_full_export_supplements_after_declared_tables(tmp_path: Path) -> None:
    """CSV: supplement tables land after declared tables, declaration order."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, supplements
        )

    assert [t.name for t in report.tables] == ["dim_actor", "region_code", "rate_tier"]
    dim_actor, region_code, rate_tier = report.tables
    assert dim_actor.supplement is None
    assert dim_actor.keys is None
    assert region_code.supplement == SupplementSource(
        file="region_code.csv", sha256=_REGION_CODE_SHA256
    )
    assert region_code.keys is None
    assert region_code.row_count == 2
    assert rate_tier.supplement == SupplementSource(file=None, sha256=None)
    assert rate_tier.keys is None
    assert rate_tier.row_count == 2
    for name in ("dim_actor", "region_code", "rate_tier"):
        assert (out_dir / f"{name}.csv").exists()


def test_duckdb_full_export_supplements_after_declared_tables(tmp_path: Path) -> None:
    """DuckDB: same table order, provenance, and row counts as CSV."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            None,
            discard_notice_sink,
            None,
            supplements,
        )

    assert [t.name for t in report.tables] == ["dim_actor", "region_code", "rate_tier"]
    dim_actor, region_code, rate_tier = report.tables
    assert dim_actor.supplement is None
    assert region_code.supplement == SupplementSource(
        file="region_code.csv", sha256=_REGION_CODE_SHA256
    )
    assert rate_tier.supplement == SupplementSource(file=None, sha256=None)
    assert region_code.row_count == 2
    assert rate_tier.row_count == 2

    conn = duckdb.connect(str(out_path), read_only=True)
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    conn.close()
    assert {"dim_actor", "region_code", "rate_tier"}.issubset(tables)


def test_zero_row_supplement_reports_zero_row_count(tmp_path: Path) -> None:
    """A `rows: []` supplement reports row_count 0, still written."""
    emit_dir = _build_emit(tmp_path / "emit")
    decl = SupplementDecl(name="empty_supplement", columns={"code": "VARCHAR"}, rows=[])
    config = _make_config([decl])
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, tmp_path)
        report = export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, supplements
        )

    empty_table = next(t for t in report.tables if t.name == "empty_supplement")
    assert empty_table.row_count == 0
    assert (out_dir / "empty_supplement.csv").exists()


# ---------------------------------------------------------------------------
# Written-value round-trip: NULL / DATE / DECIMAL
# ---------------------------------------------------------------------------


def test_csv_round_trip_null_date_decimal(tmp_path: Path) -> None:
    """CSV: NULL renders empty; DATE/DECIMAL render the writer's pinned forms."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _make_config([_rate_tier_decl()])
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, tmp_path)
        export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, supplements
        )

    rows = list(
        csv_module.DictReader((out_dir / "rate_tier.csv").read_text().splitlines())
    )
    premium = next(r for r in rows if r["tier"] == "premium")
    standard = next(r for r in rows if r["tier"] == "standard")
    assert premium["capacity"] == ""
    assert premium["effective_date"] == "2024-06-01"
    assert premium["rate"] == "2.25"
    assert standard["rate"] == "1.50"
    assert standard["effective_date"] == "2024-01-01"


def test_duckdb_round_trip_null_date_decimal(tmp_path: Path) -> None:
    """DuckDB: NULL round-trips to None; DATE/DECIMAL to native Python types."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _make_config([_rate_tier_decl()])
    out_path = tmp_path / "out.duckdb"

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, tmp_path)
        export_dimensional(
            emit,
            config,
            out_path,
            "duckdb",
            None,
            discard_notice_sink,
            None,
            supplements,
        )

    conn = duckdb.connect(str(out_path), read_only=True)
    rows = conn.execute(
        'SELECT tier, capacity, rate, effective_date FROM "rate_tier" ORDER BY tier'
    ).fetchall()
    conn.close()
    by_tier = {row[0]: row for row in rows}
    assert by_tier["premium"][1] is None
    assert by_tier["premium"][2] == Decimal("2.25")
    assert by_tier["premium"][3] == date(2024, 6, 1)
    assert by_tier["standard"][2] == Decimal("1.50")
    assert by_tier["standard"][3] == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_csv_export_idempotent_bytes(tmp_path: Path) -> None:
    """Two CSV exports of the same config produce byte-identical files."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir1 = tmp_path / "run1"
    out_dir1.mkdir()
    out_dir2 = tmp_path / "run2"
    out_dir2.mkdir()

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir1, "csv", None, discard_notice_sink, None, supplements
        )
    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir2, "csv", None, discard_notice_sink, None, supplements
        )

    for name in ("dim_actor", "region_code", "rate_tier"):
        assert (out_dir1 / f"{name}.csv").read_bytes() == (
            out_dir2 / f"{name}.csv"
        ).read_bytes()


# ---------------------------------------------------------------------------
# Manifest: supplement provenance + embedded config
# ---------------------------------------------------------------------------


def test_manifest_v3_supplement_provenance_and_embedded_config(
    tmp_path: Path,
) -> None:
    """manifest_format_version == 4; tables[].supplement per table; author
    prose forwarded; keys null; config.supplements embedded with declared file."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, supplements
        )

    _readme_path, manifest_path = companion_artifact_paths(
        out_dir, "dimensional", "csv"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_format_version"] == 4

    by_name = {t["name"]: t for t in manifest["tables"]}
    assert by_name["dim_actor"]["supplement"] is None
    assert by_name["region_code"]["supplement"] == {
        "file": "region_code.csv",
        "sha256": _REGION_CODE_SHA256,
    }
    assert by_name["rate_tier"]["supplement"] == {"file": None, "sha256": None}

    region_code_json = by_name["region_code"]
    assert (
        region_code_json["description"]
        == "Sales-region codes used by account management."
    )
    assert region_code_json["primary_key"] is None
    assert region_code_json["unique"] is None
    code_column = next(c for c in region_code_json["columns"] if c["name"] == "code")
    assert code_column["description"] == "ISO region code."
    assert code_column["unit"] is None
    assert code_column["enum_options"] is None

    rate_tier_json = by_name["rate_tier"]
    assert rate_tier_json["description"] == "Rate tiers offered to accounts."
    assert rate_tier_json["primary_key"] is None
    assert rate_tier_json["unique"] is None

    config_supplements = manifest["config"]["supplements"]
    files = {entry["name"]: entry["file"] for entry in config_supplements}
    assert files == {"region_code": "region_code.csv", "rate_tier": None}


# ---------------------------------------------------------------------------
# README: supplement section + overlay
# ---------------------------------------------------------------------------


def test_readme_renders_supplement_table_section(tmp_path: Path) -> None:
    """The README carries a per-table section for each supplement, prose forwarded."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir, "csv", None, discard_notice_sink, None, supplements
        )

    readme_path, _manifest_path = companion_artifact_paths(
        out_dir, "dimensional", "csv"
    )
    readme_text = readme_path.read_text(encoding="utf-8")
    assert "### region_code" in readme_text
    assert "### rate_tier" in readme_text
    assert "Sales-region codes used by account management." in readme_text
    assert "ISO region code." in readme_text
    assert "Rate tiers offered to accounts." in readme_text


def test_overlay_table_slot_naming_supplement_validates_and_renders(
    tmp_path: Path,
) -> None:
    """An overlay `## table: <supplement>` slot validates against the
    supplement's output name and its note renders in that table's section."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    overlay_path = tmp_path / "overlay.md"
    overlay_path.write_text(
        "## table: region_code\n\nAuthor note on region_code.\n", encoding="utf-8"
    )
    overlay = load_readme_overlay(overlay_path)

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            config,
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay,
            supplements,
        )

    readme_path, _manifest_path = companion_artifact_paths(
        out_dir, "dimensional", "csv"
    )
    readme_text = readme_path.read_text(encoding="utf-8")
    assert "Author note on region_code." in readme_text


def test_overlay_table_slot_naming_unknown_table_refused(tmp_path: Path) -> None:
    """An overlay naming a table neither declared nor a supplement is refused."""
    emit_dir = _build_emit(tmp_path / "emit")
    config, _config_dir, supplements = _happy_config_and_supplements(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    overlay_path = tmp_path / "overlay.md"
    overlay_path.write_text("## table: not_a_table\n\nNote.\n", encoding="utf-8")
    overlay = load_readme_overlay(overlay_path)

    with open_emit(emit_dir) as emit:
        with pytest.raises(ReadmeOverlayUnknownTable):
            export_dimensional(
                emit,
                config,
                out_dir,
                "csv",
                None,
                discard_notice_sink,
                overlay,
                supplements,
            )
    assert list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# SupplementSourceIsOutput: CSV / DuckDB / manifest-path collisions
# ---------------------------------------------------------------------------


def test_source_is_output_csv_own_output_file(tmp_path: Path) -> None:
    """A file supplement whose source sits at its own CSV output path is refused."""
    emit_dir = _build_emit(tmp_path / "emit")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    config = _make_config([_region_code_decl()])
    supplements = load_supplements(config, out_dir)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
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

    assert not (out_dir / "dim_actor.csv").exists()
    readme_path, manifest_path = companion_artifact_paths(out_dir, "dimensional", "csv")
    assert not readme_path.exists()
    assert not manifest_path.exists()


def test_source_is_output_duckdb_out_path(tmp_path: Path) -> None:
    """A file supplement whose source sits at the DuckDB `out` path is refused."""
    emit_dir = _build_emit(tmp_path / "emit")
    db_dir = tmp_path / "db_dir"
    db_dir.mkdir()
    out_path = db_dir / "warehouse.duckdb"
    out_path.write_text(_REGION_CODE_CSV, encoding="utf-8")
    config = _make_config([_region_code_decl(file="warehouse.duckdb")])
    supplements = load_supplements(config, db_dir)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
            export_dimensional(
                emit,
                config,
                out_path,
                "duckdb",
                None,
                discard_notice_sink,
                None,
                supplements,
            )

    assert out_path.read_text(encoding="utf-8") == _REGION_CODE_CSV
    readme_path, manifest_path = companion_artifact_paths(
        out_path, "dimensional", "duckdb"
    )
    assert not readme_path.exists()
    assert not manifest_path.exists()


def test_source_is_output_manifest_path(tmp_path: Path) -> None:
    """A file supplement whose source sits at the manifest's own path is refused."""
    emit_dir = _build_emit(tmp_path / "emit")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _readme_path, manifest_path = companion_artifact_paths(
        out_dir, "dimensional", "csv"
    )
    manifest_path.write_text(_REGION_CODE_CSV, encoding="utf-8")
    config = _make_config([_region_code_decl(file=manifest_path.name)])
    supplements = load_supplements(config, out_dir)

    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementSourceIsOutput):
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

    assert not (out_dir / "dim_actor.csv").exists()
    assert manifest_path.read_text(encoding="utf-8") == _REGION_CODE_CSV


# ---------------------------------------------------------------------------
# SupplementValueInvalid: refusal leaves the output empty
# ---------------------------------------------------------------------------


def test_supplement_value_invalid_leaves_output_empty(tmp_path: Path) -> None:
    """A bad-cell supplement refuses before any declared table is written."""
    emit_dir = _build_emit(tmp_path / "emit")
    bad_decl = SupplementDecl(
        name="bad_supplement",
        columns={"tier": "VARCHAR", "capacity": "BIGINT"},
        rows=[{"tier": "standard", "capacity": "not-a-number"}],
    )
    config = _make_config([bad_decl])
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        supplements = load_supplements(config, tmp_path)
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


# ---------------------------------------------------------------------------
# No supplements: unaffected export
# ---------------------------------------------------------------------------


def test_no_supplements_output_unaffected(tmp_path: Path) -> None:
    """supplements=() exports only the declared tables, exactly as before."""
    emit_dir = _build_emit(tmp_path / "emit")
    config = _make_config(None)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            config,
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            None,
            supplements=(),
        )

    assert [t.name for t in report.tables] == ["dim_actor"]
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "dim_actor.csv",
        "dimensional-manifest.json",
        "dimensional-readme.md",
    ]
