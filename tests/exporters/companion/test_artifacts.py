"""Tests for the mode-neutral companion writer's placement/prefix rules and
the census-exclusion predicate: write_companion_artifacts and
is_companion_artifact_name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exporters.companion._fixtures import (
    documented_actor_table_report,
    write_documented_emit,
    write_minimal_emit,
)
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.companion.artifacts import (
    is_companion_artifact_name,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.query_spec import ExportReport, TableReport
from fabulexa_forge.reader.emit import open_emit


def _report(table_name: str = "patients") -> ExportReport:
    """A minimal one-table report -- placement/prefix tests never inspect it."""
    return ExportReport(
        tables=(
            TableReport(
                name=table_name,
                columns=(("id", "BIGINT"),),
                row_count=1,
                keys=None,
                provenance={},
                kind_values={},
                author_descriptions={},
                author_table_description=None,
                event_log=False,
            ),
        )
    )


def _write_artifacts(
    emit_dir: Path, out_dir: Path, *, table_name: str = "patients"
) -> None:
    """Call write_companion_artifacts against a directory (csv) target."""
    with open_emit(emit_dir) as emit:
        write_companion_artifacts(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=_report(table_name),
            overlay=None,
            target=out_dir,
            windowed=None,
        )


# ---------------------------------------------------------------------------
# Placement / prefix
# ---------------------------------------------------------------------------


def test_directory_target_places_prefixed_files_inside_it(tmp_path: Path) -> None:
    """A directory target gets '<mode>-readme.md' / '<mode>-manifest.json' inside it."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    _write_artifacts(emit_dir, out_dir)

    assert (out_dir / "base-readme.md").exists()
    assert (out_dir / "base-manifest.json").exists()


def test_duckdb_target_places_prefixed_siblings(tmp_path: Path) -> None:
    """A .duckdb file target gets '<db-stem>-<mode>-*' siblings beside it."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    db_path = out_dir / "warehouse.duckdb"

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

    assert (out_dir / "warehouse-base-readme.md").exists()
    assert (out_dir / "warehouse-base-manifest.json").exists()


def test_second_call_overwrites_unconditionally(tmp_path: Path) -> None:
    """A second write_companion_artifacts call overwrites both files, not
    merging or appending."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    _write_artifacts(emit_dir, out_dir, table_name="patients")
    first_readme = (out_dir / "base-readme.md").read_text(encoding="utf-8")

    _write_artifacts(emit_dir, out_dir, table_name="visits")
    second_readme = (out_dir / "base-readme.md").read_text(encoding="utf-8")

    assert "patients" in first_readme
    assert "patients" not in second_readme
    assert "visits" in second_readme


# ---------------------------------------------------------------------------
# is_companion_artifact_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["dimensional", "source", "base"])
@pytest.mark.parametrize("suffix", ["readme.md", "manifest.json"])
def test_known_mode_suffix_combinations_are_companion_names(
    mode: str, suffix: str
) -> None:
    """All six mode x suffix combinations are recognized companion names."""
    assert is_companion_artifact_name(f"{mode}-{suffix}") is True


@pytest.mark.parametrize(
    "name",
    ["streaming-readme.md", "dimensional-readme.txt", "foo.csv", ".hidden"],
)
def test_unrecognized_names_are_not_companion_names(name: str) -> None:
    """Names outside the six known combinations are not companion names."""
    assert is_companion_artifact_name(name) is False


# ---------------------------------------------------------------------------
# Failure surface
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inertness -- documentation presence never reshapes the table set
# ---------------------------------------------------------------------------


def _write_documented_manifest(
    tmp_path: Path, subdir: str, *, documented: bool
) -> dict[str, object]:
    """Write companion artifacts over `write_documented_emit`'s fixture
    (documented or stripped) and return the parsed manifest document."""
    emit_dir = tmp_path / subdir / "emit"
    emit_dir.mkdir(parents=True)
    write_documented_emit(emit_dir, documented=documented)
    out_dir = tmp_path / subdir / "out"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        write_companion_artifacts(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=ExportReport(tables=(documented_actor_table_report(),)),
            overlay=None,
            target=out_dir,
            windowed=None,
        )
    return json.loads((out_dir / "base-manifest.json").read_text(encoding="utf-8"))


def test_documentation_presence_does_not_reshape_the_table_set(
    tmp_path: Path,
) -> None:
    """Whether the sidecar carries documentation or not, the manifest's
    table/column names, types, keys, and row counts are identical -- only
    the description/unit/enum_options fields vary."""
    documented = _write_documented_manifest(tmp_path, "documented", documented=True)
    stripped = _write_documented_manifest(tmp_path, "stripped", documented=False)

    def _structural_fields(table: dict[str, object]) -> dict[str, object]:
        columns = table["columns"]
        assert isinstance(columns, list)
        return {
            "name": table["name"],
            "primary_key": table["primary_key"],
            "unique": table["unique"],
            "row_count": table["row_count"],
            "columns": [{"name": c["name"], "type": c["type"]} for c in columns],
        }

    documented_tables = documented["tables"]
    stripped_tables = stripped["tables"]
    assert isinstance(documented_tables, list)
    assert isinstance(stripped_tables, list)
    assert [_structural_fields(t) for t in documented_tables] == [
        _structural_fields(t) for t in stripped_tables
    ]

    documented_columns = documented_tables[0]["columns"]
    stripped_columns = stripped_tables[0]["columns"]
    assert isinstance(documented_columns, list)
    assert isinstance(stripped_columns, list)
    documented_full_name = next(
        c for c in documented_columns if c["name"] == "full_name"
    )
    stripped_full_name = next(c for c in stripped_columns if c["name"] == "full_name")
    assert documented_full_name["description"] is not None
    assert stripped_full_name["description"] is None


def test_unwritable_target_raises_export_runtime_error(tmp_path: Path) -> None:
    """A target whose directory does not exist fails the write with
    ExportRuntimeError."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    missing_dir = tmp_path / "does-not-exist"

    with (
        open_emit(emit_dir) as emit,
        pytest.raises(ExportRuntimeError, match="failed to write companion artifacts"),
    ):
        write_companion_artifacts(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="csv",
            anchor=None,
            report=_report(),
            overlay=None,
            target=missing_dir,
            windowed=None,
        )
