"""Cross-mode full-export companion-artifact integration tests.

Exercises `export_dimensional` / `export_source` / `export_base`'s Phase-3
threading end to end -- overlay validation immediately post-compile,
`write_companion_artifacts` after data delivery, and the returned
`ExportReport`'s fidelity to what the companion manifest and README record --
rather than any one mode's own engine-test suite (which keeps its existing
dataset-shape assertions unchanged).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from _support.notices import discard_notice_sink

from exporters._emit_fixtures import build_test_emit
from exporters.base._base_fixtures import build_base_keys_emit, build_base_test_emit
from exporters.source._source_fixtures import (
    build_source_keys_emit,
    build_source_test_emit,
)
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    BaseConfig,
    ColumnDecl,
    DimensionalConfig,
    ExportConfig,
    SourceConfig,
    SourceDecl,
    SourceTableDecl,
    TableDecl,
)
from fabulexa_forge.errors import ReadmeOverlayUnknownTable
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def _dimensional_config(*, readme_overlay: str | None = None) -> ExportConfig:
    """A one-table `dim_entity` dimensional config over `build_test_emit`'s
    `entity` kind."""
    return ExportConfig(
        mode="dimensional",
        readme_overlay=readme_overlay,
        dimensional=DimensionalConfig(
            tables=[
                TableDecl(
                    name="dim_entity",
                    role="dim",
                    scd="type1",
                    source=SourceDecl(grain="records", kind="entity"),
                    key=["id"],
                    columns=[ColumnDecl(name="id", **{"from": "record_id"})],
                )
            ]
        ),
    )


def _source_config(
    *, declare_keys: bool = False, readme_overlay: str | None = None
) -> ExportConfig:
    """A one-table `location` source config over `build_source_test_emit`."""
    return ExportConfig(
        mode="source",
        readme_overlay=readme_overlay,
        source=SourceConfig(
            tables=(SourceTableDecl(name="location", kind="location"),),
            declare_keys=declare_keys,
        ),
    )


def _source_keys_config(*, declare_keys: bool) -> ExportConfig:
    """A one-table `visit` source config over `build_source_keys_emit`."""
    return ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(SourceTableDecl(name="visit", kind="visit"),),
            declare_keys=declare_keys,
        ),
    )


def _emit_subdir(tmp_path: Path) -> Path:
    """A freshly created `tmp_path/emit` directory for an emit builder to
    write into (the builders assume an existing directory)."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    return emit_dir


def _resolve_anchor(emit: "Emit") -> "EffectiveAnchor | None":
    """Resolve the effective anchor exactly as `cmd_export` would, with no
    CLI/config overrides."""
    return resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_manifest_matches_report(
    manifest: dict[str, object], report: "ExportReport"
) -> None:
    """The manifest's `tables` entries transcribe `report` exactly: name,
    ordered columns, row count, and declared-keys presence/shape."""
    entries = manifest["tables"]
    assert isinstance(entries, list)
    assert len(entries) == len(report.tables)
    for entry, table in zip(entries, report.tables, strict=True):
        assert entry["name"] == table.name
        assert [
            {"name": col["name"], "type": col["type"]} for col in entry["columns"]
        ] == [{"name": name, "type": type_text} for name, type_text in table.columns]
        assert entry["row_count"] == table.row_count
        if table.keys is None:
            assert entry["primary_key"] is None
            assert entry["unique"] is None
        else:
            assert entry["primary_key"] == list(table.keys.primary_key)
            assert entry["unique"] == [list(cols) for cols in table.keys.unique]


# ---------------------------------------------------------------------------
# Each mode x csv: both artifacts written, dataset unchanged
# ---------------------------------------------------------------------------


def test_dimensional_csv_writes_dataset_and_both_artifacts(tmp_path: Path) -> None:
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_dimensional(
            emit,
            _dimensional_config(),
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay=None,
        )

    assert (out_dir / "dim_entity.csv").exists()
    assert (out_dir / "dimensional-readme.md").exists()
    assert (out_dir / "dimensional-manifest.json").exists()
    assert {t.name: t.row_count for t in report.tables} == {"dim_entity": 2}
    _assert_manifest_matches_report(
        _read_json(out_dir / "dimensional-manifest.json"), report
    )


def test_source_csv_writes_dataset_and_both_artifacts(tmp_path: Path) -> None:
    emit_dir = build_source_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        anchor = _resolve_anchor(emit)
        report = export_source(
            emit,
            _source_config(),
            out_dir,
            "csv",
            anchor,
            discard_notice_sink,
            overlay=None,
        )

    assert (out_dir / "location.csv").exists()
    assert (out_dir / "source-readme.md").exists()
    assert (out_dir / "source-manifest.json").exists()
    _assert_manifest_matches_report(
        _read_json(out_dir / "source-manifest.json"), report
    )


def test_base_csv_writes_dataset_and_both_artifacts(tmp_path: Path) -> None:
    emit_dir = build_base_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with open_emit(emit_dir) as emit:
        report = export_base(
            emit,
            ExportConfig(mode="base"),
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay=None,
        )

    assert (out_dir / "patient.csv").exists()
    assert (out_dir / "base-readme.md").exists()
    assert (out_dir / "base-manifest.json").exists()
    assert {t.name: t.row_count for t in report.tables} == {"patient": 3}
    _assert_manifest_matches_report(_read_json(out_dir / "base-manifest.json"), report)


# ---------------------------------------------------------------------------
# Duckdb: <db-stem>-<mode>-* siblings
# ---------------------------------------------------------------------------


def test_dimensional_duckdb_writes_db_stem_siblings(tmp_path: Path) -> None:
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    db_path = tmp_path / "warehouse.duckdb"

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            _dimensional_config(),
            db_path,
            "duckdb",
            None,
            discard_notice_sink,
            overlay=None,
        )

    assert db_path.exists()
    assert (tmp_path / "warehouse-dimensional-readme.md").exists()
    assert (tmp_path / "warehouse-dimensional-manifest.json").exists()


# ---------------------------------------------------------------------------
# Manifest fidelity: dimensional null keys, declared-keys base/source
# ---------------------------------------------------------------------------


def test_dimensional_manifest_entries_carry_null_keys(tmp_path: Path) -> None:
    """Dimensional carries no `declare_keys` field: every manifest entry's
    keys are null, csv or duckdb."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            _dimensional_config(),
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay=None,
        )
        export_dimensional(
            emit,
            _dimensional_config(),
            db_path,
            "duckdb",
            None,
            discard_notice_sink,
            overlay=None,
        )

    for manifest_path in (
        out_dir / "dimensional-manifest.json",
        tmp_path / "wh-dimensional-manifest.json",
    ):
        manifest = _read_json(manifest_path)
        entries = manifest["tables"]
        assert isinstance(entries, list)
        assert entries
        for entry in entries:
            assert entry["primary_key"] is None
            assert entry["unique"] is None


def test_base_declare_keys_duckdb_manifest_carries_keys(tmp_path: Path) -> None:
    """A declared-keys base duckdb export's manifest carries the resolved
    keys per table."""
    emit_dir = build_base_keys_emit(_emit_subdir(tmp_path))
    db_path = tmp_path / "wh.duckdb"
    config = ExportConfig(mode="base", base=BaseConfig(declare_keys=True))

    with open_emit(emit_dir) as emit:
        report = export_base(
            emit, config, db_path, "duckdb", None, discard_notice_sink, overlay=None
        )

    assert all(table.keys is not None for table in report.tables)
    manifest = _read_json(tmp_path / "wh-base-manifest.json")
    _assert_manifest_matches_report(manifest, report)


def test_source_declare_keys_duckdb_manifest_carries_keys(tmp_path: Path) -> None:
    """A declared-keys source duckdb export's manifest carries the resolved
    keys for the claimed table."""
    emit_dir = build_source_keys_emit(_emit_subdir(tmp_path))
    db_path = tmp_path / "wh.duckdb"

    with open_emit(emit_dir) as emit:
        anchor = _resolve_anchor(emit)
        report = export_source(
            emit,
            _source_keys_config(declare_keys=True),
            db_path,
            "duckdb",
            anchor,
            discard_notice_sink,
            overlay=None,
        )

    assert report.tables[0].keys is not None
    manifest = _read_json(tmp_path / "wh-source-manifest.json")
    _assert_manifest_matches_report(manifest, report)


# ---------------------------------------------------------------------------
# Overlay: note rendering, unknown-table refusal
# ---------------------------------------------------------------------------


def test_overlay_note_renders_into_its_table_section(tmp_path: Path) -> None:
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    overlay = ReadmeOverlay(
        overview="Export-level overview.",
        table_notes={"dim_entity": "Custom note for dim_entity."},
    )

    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit,
            _dimensional_config(),
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay=overlay,
        )

    readme = (out_dir / "dimensional-readme.md").read_text(encoding="utf-8")
    assert "Export-level overview." in readme
    heading_index = readme.index("### dim_entity")
    note_index = readme.index("Custom note for dim_entity.")
    assert heading_index < note_index


def test_overlay_unknown_table_raises_and_leaves_target_empty(tmp_path: Path) -> None:
    """An overlay `table:` slot naming a table the plan won't produce raises
    before any write -- no dataset, no companion artifacts."""
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    overlay = ReadmeOverlay(overview=None, table_notes={"no_such_table": "note"})

    with (
        open_emit(emit_dir) as emit,
        pytest.raises(ReadmeOverlayUnknownTable),
    ):
        export_dimensional(
            emit,
            _dimensional_config(),
            out_dir,
            "csv",
            None,
            discard_notice_sink,
            overlay=overlay,
        )

    assert not any(out_dir.iterdir())


# ---------------------------------------------------------------------------
# Re-run byte-identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["csv", "duckdb"])
def test_rerun_is_byte_identical_for_both_artifacts(tmp_path: Path, fmt: str) -> None:
    emit_dir = build_test_emit(_emit_subdir(tmp_path))
    out = tmp_path / ("out" if fmt == "csv" else "wh.duckdb")
    if fmt == "csv":
        out.mkdir()

    fmt_lit = cast("Literal['csv', 'duckdb']", fmt)

    def _run() -> tuple[bytes, bytes]:
        if fmt == "duckdb":
            # A full-export duckdb write creates its tables fresh; re-running
            # into the same existing warehouse file is not this write path's
            # contract (that is the incremental writer's job) -- a fresh
            # target each run mirrors how a full export is actually invoked.
            out.unlink(missing_ok=True)
        with open_emit(emit_dir) as emit:
            export_dimensional(
                emit,
                _dimensional_config(),
                out,
                fmt_lit,
                None,
                discard_notice_sink,
                overlay=None,
            )
        if fmt == "csv":
            readme_path = out / "dimensional-readme.md"
            manifest_path = out / "dimensional-manifest.json"
        else:
            readme_path = tmp_path / "wh-dimensional-readme.md"
            manifest_path = tmp_path / "wh-dimensional-manifest.json"
        return readme_path.read_bytes(), manifest_path.read_bytes()

    first_readme, first_manifest = _run()
    second_readme, second_manifest = _run()

    assert first_readme == second_readme
    assert first_manifest == second_manifest
