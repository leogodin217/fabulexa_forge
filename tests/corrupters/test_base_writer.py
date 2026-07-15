"""Tests for `corrupters.base_writer.write_base_emit`."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest
from _support.sidecar_builder import write_emit

from fabulexa_forge.corrupters.base_writer import write_base_emit
from fabulexa_forge.corrupters.state import CorruptState, WorkingTable
from fabulexa_forge.errors import ExportRuntimeError

from ._helpers import column_spec, table_spec, working_table


def _source_sidecar(tmp_path: Path) -> dict[str, object]:
    """Build `write_base_emit`'s source-sidecar template through `write_emit`.

    The one sidecar authority, even though this dict is used purely as an
    in-memory verbatim-fields template (`write_base_emit`'s
    `source_sidecar_raw` argument) -- this file never opens it via
    `open_emit`. Written to a scratch directory and read back, rather than
    typed by hand.
    """
    src_dir = tmp_path / "_source"
    src_dir.mkdir()
    placeholder_table: dict[str, object] = {
        "name": "placeholder",
        "category": "fixed",
        "columns": [{"name": "fork_path", "type": "VARCHAR"}],
        "rows": 0,
    }
    write_emit(
        src_dir,
        tables=[placeholder_table],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "pinned_ids": {"actor": {"alice": "a001"}},
            "enum_domains": {"actor": {"status": ["active", "discharged"]}},
            "record_roles": {"actor": "fact"},
        },
    )
    return json.loads((src_dir / "base.json").read_text(encoding="utf-8"))


def _one_table_state() -> CorruptState:
    spec = table_spec(
        "records__actor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec(
                "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
            ),
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
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

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
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == source_sidecar["base_format_version"]
    assert sidecar["branches"] == source_sidecar["branches"]
    assert sidecar["runtime"] == source_sidecar["runtime"]
    assert sidecar["pinned_ids"] == source_sidecar["pinned_ids"]
    assert sidecar["enum_domains"] == source_sidecar["enum_domains"]
    assert sidecar["record_roles"] == source_sidecar["record_roles"]


def test_canonical_row_order(tmp_path: Path) -> None:
    """Rows land in canonical content order (ascending, by every column)."""
    state = _one_table_state()
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

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
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

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
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

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
            column_spec(
                "prop__full_name",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            ),
        ),
        record_kind="actor",
    )
    wt = working_table(
        evolved_spec,
        [{"fork_path": "trunk", "record_id": "a001", "prop__full_name": "Alice"}],
    )
    state = CorruptState(tables={"records__actor": wt})
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    columns_by_name = {c["name"]: c for c in sidecar["tables"][0]["columns"]}
    assert "prop__name" not in columns_by_name  # dropped/renamed-away, absent
    assert columns_by_name["prop__full_name"]["history_tracked"] is True


def test_fixed_category_table_entry_omits_record_kind_and_property(
    tmp_path: Path,
) -> None:
    """A fixed-category table (record_kind/property None -- `history`) writes a
    tables[] entry carrying category "fixed" and no record_kind/property keys."""
    history_spec = table_spec(
        "history",
        "fixed",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("kind", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("property", "VARCHAR"),
            column_spec("sim_time", "BIGINT"),
            column_spec("value", "VARCHAR"),
        ),
    )
    wt = working_table(
        history_spec,
        [
            {
                "fork_path": "trunk",
                "kind": "actor",
                "record_id": "a001",
                "property": "status",
                "sim_time": 5,
                "value": "active",
            }
        ],
    )
    state = CorruptState(tables={"history": wt})
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    write_base_emit(state, source_sidecar, out_dir)

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    (table,) = sidecar["tables"]
    assert table["name"] == "history"
    assert table["category"] == "fixed"
    assert table["rows"] == 1
    assert "record_kind" not in table
    assert "property" not in table
    assert [c["name"] for c in table["columns"]] == [
        "fork_path",
        "kind",
        "record_id",
        "property",
        "sim_time",
        "value",
    ]


def test_determinism_byte_identical(tmp_path: Path) -> None:
    state1 = _one_table_state()
    state2 = _one_table_state()
    source_sidecar = _source_sidecar(tmp_path)
    out_dir1 = tmp_path / "out1"
    out_dir2 = tmp_path / "out2"
    write_base_emit(state1, source_sidecar, out_dir1)
    write_base_emit(state2, source_sidecar, out_dir2)

    assert (out_dir1 / "base.json").read_bytes() == (
        out_dir2 / "base.json"
    ).read_bytes()


def test_write_failure_surfaces_export_runtime_error(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Make run.duckdb a directory so duckdb.connect() fails to open it as a file.
    (out_dir / "run.duckdb").mkdir()

    state = _one_table_state()
    source_sidecar = _source_sidecar(tmp_path)
    with pytest.raises(ExportRuntimeError):
        write_base_emit(state, source_sidecar, out_dir)


def _two_table_state() -> CorruptState:
    state = _one_table_state()
    doctor_spec = table_spec(
        "records__doctor",
        "records",
        (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
        record_kind="doctor",
    )
    doctors = working_table(doctor_spec, [{"fork_path": "trunk", "record_id": "d001"}])
    tables = dict(state.tables)
    tables["records__doctor"] = doctors
    return CorruptState(tables=tables)


def test_mid_write_failure_removes_partial_run_duckdb_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while writing a later table removes the partial run.duckdb,
    so a retry into the same out_dir is not refused as an existing emit."""
    from fabulexa_forge.corrupters import base_writer as base_writer_module

    real_canonical_rows = base_writer_module._canonical_rows
    calls: list[int] = []

    def flaky_canonical_rows(working: WorkingTable) -> pa.Table:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real_canonical_rows(working)

    monkeypatch.setattr(base_writer_module, "_canonical_rows", flaky_canonical_rows)

    state = _two_table_state()
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"
    with pytest.raises(ExportRuntimeError, match="disk full"):
        write_base_emit(state, source_sidecar, out_dir)
    assert not (out_dir / "run.duckdb").exists()
    assert not (out_dir / "base.json").exists()

    monkeypatch.undo()
    write_base_emit(state, source_sidecar, out_dir)  # retry is not blocked
    assert (out_dir / "run.duckdb").exists()
    assert (out_dir / "base.json").exists()


def test_base_json_write_failure_removes_run_duckdb(tmp_path: Path) -> None:
    """A base.json write failure removes the (complete but sidecar-less, hence
    unusable) run.duckdb rather than leaving half an emit behind."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Make base.json a directory so write_text() fails after run.duckdb landed.
    (out_dir / "base.json").mkdir()

    state = _one_table_state()
    source_sidecar = _source_sidecar(tmp_path)
    with pytest.raises(ExportRuntimeError, match="base.json"):
        write_base_emit(state, source_sidecar, out_dir)
    assert not (out_dir / "run.duckdb").exists()


def test_bundle_sourced_table_name_with_embedded_quote_is_written_safely(
    tmp_path: Path,
) -> None:
    """A sidecar-sourced table name containing a double-quote (bundle names
    cannot be pattern-gated) lands as a literal catalog name — the quote never
    breaks out of the CREATE TABLE / DESCRIBE identifier position."""
    evil_name = "records__actor\" ; ATTACH '/tmp/x.db' AS x; --"
    spec = table_spec(
        evil_name,
        "records",
        (column_spec("fork_path", "VARCHAR"), column_spec("record_id", "VARCHAR")),
        record_kind="actor",
    )
    wt = working_table(spec, [{"fork_path": "trunk", "record_id": "a001"}])
    state = CorruptState(tables={evil_name: wt})
    source_sidecar = _source_sidecar(tmp_path)
    out_dir = tmp_path / "out"

    write_base_emit(state, source_sidecar, out_dir)

    import duckdb

    conn = duckdb.connect(str(out_dir / "run.duckdb"), read_only=True)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
    finally:
        conn.close()
    assert evil_name in names

    sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    assert sidecar["tables"][0]["name"] == evil_name
