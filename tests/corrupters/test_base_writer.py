"""Tests for `corrupters.base_writer.write_base_emit`."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from fabulexa_export.corrupters.base_writer import write_base_emit
from fabulexa_export.corrupters.state import CorruptState, WorkingTable
from fabulexa_export.errors import ExportRuntimeError

from ._helpers import column_spec, table_spec, working_table

_SOURCE_SIDECAR: dict[str, object] = {
    "base_format_version": 4,
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    "tables": [{"name": "placeholder", "category": "fixed", "columns": [], "rows": 0}],
    "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
    "pinned_ids": {"actor": {"alice": "a001"}},
    "enum_domains": {"actor": {"status": ["active", "discharged"]}},
    "record_roles": {"actor": "fact"},
}


def _one_table_state() -> CorruptState:
    spec = table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__name", "VARCHAR", history_tracked=True),
            column_spec("prop__doctor_id", "VARCHAR", references="doctor"),
        ),
        record_kind="actor",
    )
    wt = working_table(
        spec,
        [
            {
                "fork_path": "trunk",
                "record_id": "a002",
                "prop__name": "Bob",
                "prop__doctor_id": "d001",
            },
            {
                "fork_path": "trunk",
                "record_id": "a001",
                "prop__name": "Alice",
                "prop__doctor_id": "d002",
            },
        ],
    )
    return CorruptState(tables={"records__actor": wt})


def test_writes_run_duckdb_and_base_json(tmp_path: Path) -> None:
    state = _one_table_state()
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    assert (out_dir / "run.duckdb").exists()
    assert (out_dir / "base.json").exists()

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    assert len(sidecar["tables"]) == 1
    table = sidecar["tables"][0]
    assert table["name"] == "records__actor"
    assert table["category"] == "records"
    assert table["record_kind"] == "actor"
    assert table["rows"] == 2
    columns_by_name = {c["name"]: c for c in table["columns"]}
    assert columns_by_name["prop__name"]["type"] == "VARCHAR"
    assert columns_by_name["prop__name"]["history_tracked"] is True
    assert columns_by_name["prop__doctor_id"]["references"] == "doctor"
    assert "references" not in columns_by_name["record_id"]
    assert "history_tracked" not in columns_by_name["record_id"]


def test_verbatim_top_level_fields(tmp_path: Path) -> None:
    state = _one_table_state()
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == _SOURCE_SIDECAR["base_format_version"]
    assert sidecar["branches"] == _SOURCE_SIDECAR["branches"]
    assert sidecar["runtime"] == _SOURCE_SIDECAR["runtime"]
    assert sidecar["pinned_ids"] == _SOURCE_SIDECAR["pinned_ids"]
    assert sidecar["enum_domains"] == _SOURCE_SIDECAR["enum_domains"]
    assert sidecar["record_roles"] == _SOURCE_SIDECAR["record_roles"]


def test_canonical_row_order(tmp_path: Path) -> None:
    """Rows land in canonical content order (ascending, by every column)."""
    state = _one_table_state()
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    import duckdb

    conn = duckdb.connect(str(out_dir / "run.duckdb"), read_only=True)
    try:
        rows = conn.execute('SELECT record_id FROM "records__actor"').fetchall()
    finally:
        conn.close()
    # a001 < a002 lexicographically -- canonical order sorts ascending.
    assert [r[0] for r in rows] == ["a001", "a002"]


def test_duplicate_row_lands_adjacent_to_original(tmp_path: Path) -> None:
    spec = table_spec(
        "records__actor",
        "records",
        (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
        record_kind="actor",
    )
    wt = working_table(
        spec,
        [
            {"fork_path": "trunk", "record_id": "a002"},
            {"fork_path": "trunk", "record_id": "a001"},
            {"fork_path": "trunk", "record_id": "a001"},  # exact duplicate
        ],
    )
    state = CorruptState(tables={"records__actor": wt})
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    import duckdb

    conn = duckdb.connect(str(out_dir / "run.duckdb"), read_only=True)
    try:
        rows = conn.execute('SELECT record_id FROM "records__actor"').fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["a001", "a001", "a002"]


def test_untouched_column_types_survive_round_trip(tmp_path: Path) -> None:
    """DATE / DECIMAL / TIMESTAMP columns keep their DuckDB type through the
    Arrow write-back."""
    spec = table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__birthday", "DATE"),
            column_spec("prop__balance", "DECIMAL(10,2)"),
        ),
        record_kind="actor",
    )
    data = pa.table(
        {
            "fork_path": pa.array(["trunk"], type=pa.string()),
            "record_id": pa.array(["a001"], type=pa.string()),
            "prop__birthday": pa.array([pa.scalar("2000-01-01").cast(pa.date32())]),
            "prop__balance": pa.array([pa.scalar(12.34).cast(pa.decimal128(10, 2))]),
        }
    )
    wt = WorkingTable(spec=spec, data=data)
    state = CorruptState(tables={"records__actor": wt})
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    columns_by_name = {c["name"]: c for c in sidecar["tables"][0]["columns"]}
    assert columns_by_name["prop__birthday"]["type"] == "DATE"
    assert columns_by_name["prop__balance"]["type"] == "DECIMAL(10,2)"


def test_renamed_column_carries_history_tracked_dropped_column_absent(
    tmp_path: Path,
) -> None:
    """references/history_tracked follow a rename with the relabeled column;
    a dropped column has no entry in the written catalog."""
    evolved_spec = table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__full_name", "VARCHAR", history_tracked=True),
        ),
        record_kind="actor",
    )
    wt = working_table(
        evolved_spec,
        [{"fork_path": "trunk", "record_id": "a001", "prop__full_name": "Alice"}],
    )
    state = CorruptState(tables={"records__actor": wt})
    out_dir = tmp_path / "out"
    write_base_emit(state, _SOURCE_SIDECAR, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    columns_by_name = {c["name"]: c for c in sidecar["tables"][0]["columns"]}
    assert "prop__name" not in columns_by_name  # dropped/renamed-away, absent
    assert columns_by_name["prop__full_name"]["history_tracked"] is True


def test_determinism_byte_identical(tmp_path: Path) -> None:
    state1 = _one_table_state()
    state2 = _one_table_state()
    out_dir1 = tmp_path / "out1"
    out_dir2 = tmp_path / "out2"
    write_base_emit(state1, _SOURCE_SIDECAR, out_dir1)
    write_base_emit(state2, _SOURCE_SIDECAR, out_dir2)

    assert (out_dir1 / "base.json").read_bytes() == (
        out_dir2 / "base.json"
    ).read_bytes()


def test_write_failure_surfaces_export_runtime_error(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Make run.duckdb a directory so duckdb.connect() fails to open it as a file.
    (out_dir / "run.duckdb").mkdir()

    state = _one_table_state()
    with pytest.raises(ExportRuntimeError):
        write_base_emit(state, _SOURCE_SIDECAR, out_dir)
