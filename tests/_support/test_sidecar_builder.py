"""Tests for the one sidecar-fixture authority: prop_column, identity_column,
and write_emit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _support.sidecar_builder import (
    UNSUPPORTED_VERSION_SENTINEL,
    identity_column,
    prop_column,
    write_emit,
)
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

_FIRINGS_TABLE: dict[str, object] = {
    "name": "firings",
    "category": "fixed",
    "columns": [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
    ],
    "rows": 0,
}

# A well-formed v6 records__actor column list: head + lifecycle tail +
# record_index, then a reference-annotated prop__ column immediately followed
# by its ref_index__ sibling (§ Contracts -- v6 layout). Shared by the
# write_emit records-shape negatives below, each of which mutates a copy.
_WELL_FORMED_RECORDS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__doctor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="doctor",
    ),
    identity_column("ref_index__doctor_id", "BIGINT"),
]


def _records_table(name: str, columns: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal records-category table spec for write_emit's tables list."""
    return {
        "name": name,
        "category": "records",
        "record_kind": "actor",
        "columns": columns,
        "rows": 0,
    }


# ---------------------------------------------------------------------------
# prop_column
# ---------------------------------------------------------------------------


def test_prop_column_rejects_tracked_without_history_tracked() -> None:
    """'tracked' with history_tracked=False raises ValueError."""
    with pytest.raises(ValueError, match="tracked"):
        prop_column(
            "prop__status",
            "VARCHAR",
            history_tracked=False,
            temporal_class="tracked",
        )


def test_prop_column_rejects_slice_only_with_history_tracked() -> None:
    """'slice_only' with history_tracked=True raises ValueError."""
    with pytest.raises(ValueError, match="slice_only"):
        prop_column(
            "prop__insurer",
            "VARCHAR",
            history_tracked=True,
            temporal_class="slice_only",
        )


def test_prop_column_builds_conformant_pair() -> None:
    """A valid pairing builds a column dict carrying both attributes."""
    column = prop_column(
        "prop__patient_id",
        "VARCHAR",
        history_tracked=True,
        temporal_class="constant",
    )
    assert column["history_tracked"] is True
    assert column["temporal_class"] == "constant"
    assert "references" not in column


def test_prop_column_includes_references_when_given() -> None:
    """references is present iff explicitly given."""
    column = prop_column(
        "prop__doctor",
        "VARCHAR",
        history_tracked=True,
        temporal_class="constant",
        references="doctor",
    )
    assert column["references"] == "doctor"


# ---------------------------------------------------------------------------
# identity_column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "duckdb_type"),
    [
        ("fork_path", "VARCHAR"),
        ("record_id", "VARCHAR"),
        ("record_index", "BIGINT"),
        ("ref_index__doctor_id", "BIGINT"),
    ],
)
def test_identity_column_emits_name_and_type_only(name: str, duckdb_type: str) -> None:
    """A name that classifies as identity emits exactly {"name", "type"}."""
    column = identity_column(name, duckdb_type)
    assert column == {"name": name, "type": duckdb_type}


@pytest.mark.parametrize("name", ["prop__x", "created_sim_time"])
def test_identity_column_rejects_non_identity_name(name: str) -> None:
    """A name that does not classify as identity (payload or lifecycle) raises."""
    with pytest.raises(ValueError, match=name):
        identity_column(name, "VARCHAR")


# ---------------------------------------------------------------------------
# write_emit
# ---------------------------------------------------------------------------


def test_write_emit_default_stamps_supported_version(tmp_path: Path) -> None:
    """Omitting base_format_version stamps SUPPORTED_BASE_FORMAT_VERSION."""
    write_emit(tmp_path, tables=[_FIRINGS_TABLE])
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == SUPPORTED_BASE_FORMAT_VERSION


def test_write_emit_schema_valid_rejects_missing_required_field(
    tmp_path: Path,
) -> None:
    """schema_valid=True names the missing required field at construction time."""
    broken_table: dict[str, object] = {
        "name": "firings",
        "category": "fixed",
        "columns": [{"name": "fork_path", "type": "VARCHAR"}],
        # 'rows' omitted -- schema-required.
    }
    with pytest.raises(ValueError, match="rows"):
        write_emit(tmp_path, tables=[broken_table], schema_valid=True)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_schema_valid_false_writes_schema_invalid_sidecar(
    tmp_path: Path,
) -> None:
    """schema_valid=False writes a fixture whose declared defect is schema-level."""
    write_emit(
        tmp_path,
        tables=[_FIRINGS_TABLE],
        base_format_version=UNSUPPORTED_VERSION_SENTINEL,
        schema_valid=False,
    )
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == UNSUPPORTED_VERSION_SENTINEL


def test_write_emit_extra_blocks_carried_verbatim(tmp_path: Path) -> None:
    """extra top-level blocks (e.g. record_roles) are carried verbatim."""
    write_emit(
        tmp_path,
        tables=[_FIRINGS_TABLE],
        extra={"record_roles": {"entity": "dimension"}},
    )
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["record_roles"] == {"entity": "dimension"}


# ---------------------------------------------------------------------------
# write_emit -- v6 records-shape assertion (records_shape_valid)
# ---------------------------------------------------------------------------


def test_write_emit_rejects_records_table_missing_record_index(
    tmp_path: Path,
) -> None:
    """A records table with no record_index names the table and column."""
    columns = [c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "record_index"]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "record_index" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_rejects_reference_prop_without_ref_index_sibling(
    tmp_path: Path,
) -> None:
    """A reference-annotated prop__ column without its ref_index__ sibling
    names the table and the missing sibling column."""
    columns = [
        c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "ref_index__doctor_id"
    ]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "ref_index__doctor_id" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_rejects_no_role_column(tmp_path: Path) -> None:
    """A column matching no records-column taxonomy role names the table and
    column."""
    columns = [
        *_WELL_FORMED_RECORDS_COLUMNS,
        {"name": "bogus_column", "type": "VARCHAR"},
    ]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "bogus_column" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_records_shape_valid_false_bypasses_shape_net(
    tmp_path: Path,
) -> None:
    """records_shape_valid=False writes a shape-defective records table
    without touching schema_valid (still True by default here)."""
    columns = [c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "record_index"]
    table = _records_table("records__actor", columns)
    write_emit(tmp_path, tables=[table], records_shape_valid=False)
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    written_columns = sidecar["tables"][0]["columns"]
    assert all(c["name"] != "record_index" for c in written_columns)
