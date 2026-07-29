#!/usr/bin/env python
"""
Demo: Shared shape (TableKeys) + DuckDB writer explicit-DDL constraint path
Sprint: presentation-keys
Phase: 2

Hand-builds two QuerySpecs over a tiny emit — one keyed, one not — writes a
DuckDB file via the shared `write_query_specs` dispatch, then:

1. Prints `duckdb_constraints()` for both tables, showing PRIMARY KEY +
   UNIQUE declared on the keyed table only.
2. Loads a claim-falsifying (duplicate primary-key) relation through
   `write_duckdb` directly and shows the loud `ExportRuntimeError` naming
   the offending table.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.exporters.query_spec import QuerySpec, TableKeys, write_query_specs
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.writers.duckdb import write_duckdb

_ENTITY_TABLE: dict[str, object] = {
    "name": "records__entity",
    "category": "records",
    "record_kind": "entity",
    "columns": [
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "name", "type": "VARCHAR"},
    ],
    "rows": 2,
}


def _write_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json pair into `emit_dir`."""
    import duckdb

    base_json = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [_ENTITY_TABLE],
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    try:
        conn.execute("CREATE TABLE records__entity (record_id VARCHAR, name VARCHAR)")
        conn.execute(
            "INSERT INTO records__entity VALUES ('e001', 'Alice'), ('e002', 'Bob')"
        )
    finally:
        conn.close()


def _print_constraints(db_path: Path, table_name: str) -> None:
    import duckdb

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT constraint_type, constraint_column_names"
            " FROM duckdb_constraints() WHERE table_name = ?",
            [table_name],
        ).fetchall()
    finally:
        conn.close()
    print(f"  {table_name}: {rows if rows else '(no declared constraints)'}")


def _write_keyed_and_unkeyed(emit_dir: Path, out_path: Path) -> None:
    """Write one keyed, one unkeyed QuerySpec through the shared dispatch."""
    specs = [
        QuerySpec(
            table_name="dim_entity",
            sql='SELECT record_id AS id, name FROM "records__entity"'
            " ORDER BY record_id",
            write_mode="create",
            view_name=None,
            view_sql=None,
            keys=TableKeys(primary_key=("id",), unique=()),
        ),
        QuerySpec(
            table_name="dim_plain",
            sql='SELECT record_id AS id, name FROM "records__entity"'
            " ORDER BY record_id",
            write_mode="create",
            view_name=None,
            view_sql=None,
        ),
    ]

    with open_emit(emit_dir) as emit:
        result = write_query_specs(emit, specs, out_path, "duckdb")

    print(f"row counts: {result}")
    print("== duckdb_constraints() ==")
    _print_constraints(out_path, "dim_entity")
    _print_constraints(out_path, "dim_plain")


def _load_falsified_claim(emit_dir: Path, out_path: Path) -> None:
    """A duplicate-id relation loaded against a claimed PRIMARY KEY fails
    loudly, naming the table."""
    print("\n== Falsified claim (duplicate primary key) ==")
    keys = {"dim_dup": TableKeys(primary_key=("id",), unique=())}
    queries = {
        "dim_dup": "SELECT 'e001' AS id, 'Alice' AS name"
        " UNION ALL SELECT 'e001' AS id, 'Alice II' AS name"
    }

    with open_emit(emit_dir) as emit:
        try:
            write_duckdb(emit, queries, out_path, keys)
        except ExportRuntimeError as exc:
            print(f"ExportRuntimeError: {exc}")


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabulexa_forge_phase2_demo_"))
    try:
        _write_emit(tmp_dir)
        out_path = tmp_dir / "out.duckdb"
        _write_keyed_and_unkeyed(tmp_dir, out_path)
        _load_falsified_claim(tmp_dir, tmp_dir / "falsified.duckdb")
        print(
            "\nSUCCESS: keyed table declares constraints, unkeyed table does not,"
            " a falsified claim fails loudly naming the table"
        )
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
