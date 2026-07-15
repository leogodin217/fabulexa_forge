"""Tests for the one sidecar-fixture authority: prop_column and write_emit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _support.sidecar_builder import (
    UNSUPPORTED_VERSION_SENTINEL,
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
