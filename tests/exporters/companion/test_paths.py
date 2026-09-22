"""Tests for companion_artifact_paths: the placement/prefix naming function
also read by the supplement source-is-output gate, and its agreement with
what write_companion_artifacts actually lands."""

from __future__ import annotations

from pathlib import Path

from exporters.companion._fixtures import write_minimal_emit
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.companion.artifacts import (
    companion_artifact_paths,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.query_spec import ExportReport, TableReport
from fabulexa_forge.reader.emit import open_emit


def test_csv_target_names_mode_prefixed_pair_inside_directory() -> None:
    """A csv target's pair lands inside the output directory, named
    `<mode>-readme.md` / `<mode>-manifest.json`."""
    readme, manifest = companion_artifact_paths(Path("/tmp/out"), "dimensional", "csv")

    assert readme == Path("/tmp/out/dimensional-readme.md")
    assert manifest == Path("/tmp/out/dimensional-manifest.json")


def test_duckdb_target_names_db_stem_prefixed_pair_as_siblings() -> None:
    """A duckdb target's pair lands beside the `.duckdb` file, named
    `<db-stem>-<mode>-readme.md` / `-manifest.json`."""
    readme, manifest = companion_artifact_paths(
        Path("/tmp/out/warehouse.duckdb"), "base", "duckdb"
    )

    assert readme == Path("/tmp/out/warehouse-base-readme.md")
    assert manifest == Path("/tmp/out/warehouse-base-manifest.json")


def _report() -> ExportReport:
    return ExportReport(
        tables=(
            TableReport(
                name="patients",
                columns=(("id", "BIGINT"),),
                row_count=1,
                keys=None,
                provenance={},
                kind_values={},
                author_descriptions={},
                author_table_description=None,
                event_log=False,
                supplement=None,
                calendar=None,
                references={},
                window_bounds={},
            ),
        )
    )


def test_write_companion_artifacts_lands_exactly_the_csv_pair(tmp_path: Path) -> None:
    """write_companion_artifacts under a csv target writes exactly the two
    paths companion_artifact_paths names — nothing else."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    readme_path, manifest_path = companion_artifact_paths(out_dir, "base", "csv")
    with open_emit(emit_dir) as emit:
        write_companion_artifacts(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=_report(),
            overlay=None,
            target=out_dir,
            windowed=None,
        )

    assert set(out_dir.iterdir()) == {readme_path, manifest_path}


def test_write_companion_artifacts_lands_exactly_the_duckdb_pair(
    tmp_path: Path,
) -> None:
    """write_companion_artifacts under a duckdb target writes exactly the
    two sibling paths companion_artifact_paths names."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    db_path = out_dir / "warehouse.duckdb"

    readme_path, manifest_path = companion_artifact_paths(db_path, "base", "duckdb")
    with open_emit(emit_dir) as emit:
        write_companion_artifacts(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="duckdb",
            anchor=None,
            report=_report(),
            overlay=None,
            target=db_path,
            windowed=None,
        )

    assert set(out_dir.iterdir()) == {readme_path, manifest_path}
