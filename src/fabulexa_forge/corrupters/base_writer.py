"""The base-emit writer: serializes a `CorruptState` to `run.duckdb` + a
regenerated `base.json`.

The one place in the package that *writes* base-format knowledge -- kept out
of the schema-agnostic `writers/` (the generic CSV/DuckDB serializers carry no
sidecar knowledge). Regenerating `base.json`'s `tables` array from the catalog
this module just wrote is what makes C2 hold by construction. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § The base-emit
writer (normative).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fabulexa_forge._sql import quote_identifier
from fabulexa_forge.corrupters.selection import build_canonical_order_clause
from fabulexa_forge.errors import ExportRuntimeError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pyarrow

    from fabulexa_forge.corrupters.state import CorruptState, WorkingTable


@dataclass(frozen=True)
class _WrittenTable:
    """One table's catalog + row count, read back from the just-written DuckDB."""

    columns: tuple[tuple[str, str], ...]
    """(column_name, duckdb_type) pairs, in the written catalog's own order."""
    rows: int
    """The row count actually written."""


def _canonical_rows(working_table: "WorkingTable") -> "pyarrow.Table":
    """Project + order a working table's content for writing.

    Registers the working Arrow as an ephemeral DuckDB relation and selects
    its columns in `WorkingTable.spec` order (source order minus any dropped
    columns), ordered by the selector's own canonical content order --
    reusing `build_canonical_order_clause`, so the written row order is the
    identical pure function of content the selector imposes elsewhere (a
    `duplicate_rows` copy lands adjacent to its original).

    Args:
        working_table: The table's final in-flight state.

    Returns:
        The table's content, columns and rows in writing order.
    """
    import duckdb

    columns_sql = ", ".join(
        quote_identifier(col.name) for col in working_table.spec.columns
    )
    order_clause = build_canonical_order_clause(working_table)
    conn = duckdb.connect(":memory:")
    try:
        conn.register("working", working_table.data)
        sql = f"SELECT {columns_sql} FROM working {order_clause}"
        return conn.execute(sql).fetch_arrow_table()
    finally:
        conn.close()


def _remove_partial_output(out_dir: "Path") -> None:
    """Best-effort removal of a failed write's partial emit files.

    A partially-written `run.duckdb` (or a dangling `base.json`) left behind
    by a failed write would trip the engine's refuses-to-overwrite guard on
    the next run with a misleading "existing emit" error; removing it keeps
    `out_dir` retryable. Removal errors are swallowed -- the original write
    failure is the error worth surfacing.

    Args:
        out_dir: The destination directory the failed write targeted.
    """
    for name in ("run.duckdb", "base.json"):
        try:
            (out_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _write_run_duckdb(
    state: "CorruptState", out_dir: "Path"
) -> dict[str, _WrittenTable]:
    """Write every working table into a fresh `out_dir/run.duckdb`.

    Tables are written in `state.tables`' own order (the engine's
    materialization order, itself source-table order -- preserved by dict
    insertion order), each in working-schema column order and canonical
    content row order (`_canonical_rows`). The read-only source is never
    touched; this always opens a fresh output file. On any mid-write failure
    the partial `run.duckdb` is removed (best-effort) before the error
    propagates, so a retry into the same `out_dir` is not blocked by the
    engine's refuses-to-overwrite guard on a file no valid emit produced.

    Args:
        state: The final working set after all operations.
        out_dir: Destination directory (already created by the caller).

    Returns:
        Per-table name, the catalog + row count read back from what was just
        written.

    Raises:
        ExportRuntimeError: Opening or writing the output DuckDB fails -- the
            writer failure domain, never the reader's `RunDatabaseError`.
    """
    import duckdb

    db_path = out_dir / "run.duckdb"
    try:
        conn = duckdb.connect(str(db_path))
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to open output DuckDB at {db_path}: {exc}"
        ) from exc

    written: dict[str, _WrittenTable] = {}
    try:
        try:
            for table_name, working_table in state.tables.items():
                try:
                    canonical = _canonical_rows(working_table)
                    conn.register("_arrow_src", canonical)
                    conn.execute(
                        f"CREATE TABLE {quote_identifier(table_name)}"
                        " AS SELECT * FROM _arrow_src"
                    )
                    conn.unregister("_arrow_src")
                    described = conn.execute(
                        f"DESCRIBE {quote_identifier(table_name)}"
                    ).fetchall()
                except Exception as exc:
                    raise ExportRuntimeError(
                        f"failed to write table '{table_name}' to {db_path}: {exc}"
                    ) from exc
                written[table_name] = _WrittenTable(
                    columns=tuple((row[0], row[1]) for row in described),
                    rows=canonical.num_rows,
                )
        finally:
            conn.close()
    except BaseException:
        _remove_partial_output(out_dir)
        raise
    return written


def _build_table_entry(
    table_name: str, working_table: "WorkingTable", written: _WrittenTable
) -> dict[str, object]:
    """Rebuild one table's `base.json` `tables[]` entry.

    `{name, type}` per column, and `rows`, come from `written` (the catalog
    and row count just written); `references` / `history_tracked` /
    `temporal_class` come from the matching `WorkingTable.spec` `ColumnSpec`,
    joined by post-drift name -- so a renamed column carries them on its
    relabeled spec, and a dropped column drops them (it is simply absent
    from `written.columns`). Each attribute is declared verbatim when the
    spec carries it and left absent otherwise -- never emitted as `null`,
    never invented for a column that carries neither. Table-level `category`
    / `record_kind` / `property` come from `working_table.spec` (C1/C3
    require them).

    Args:
        table_name: The table's (unchanged) name.
        working_table: The table's final in-flight state.
        written: The catalog + row count `_write_run_duckdb` read back for
            this table.

    Returns:
        One `base.json` `tables[]` entry.
    """
    spec = working_table.spec
    columns_by_name = {col.name: col for col in spec.columns}
    columns: list[dict[str, object]] = []
    for name, duckdb_type in written.columns:
        col_spec = columns_by_name[name]
        column_entry: dict[str, object] = {"name": name, "type": duckdb_type}
        if col_spec.references is not None:
            column_entry["references"] = col_spec.references
        if col_spec.history_tracked is not None:
            column_entry["history_tracked"] = col_spec.history_tracked
        if col_spec.temporal_class is not None:
            column_entry["temporal_class"] = col_spec.temporal_class
        columns.append(column_entry)

    entry: dict[str, object] = {
        "name": table_name,
        "category": spec.category,
        "columns": columns,
        "rows": written.rows,
    }
    if spec.record_kind is not None:
        entry["record_kind"] = spec.record_kind
    if spec.property is not None:
        entry["property"] = spec.property
    return entry


def write_base_emit(
    state: "CorruptState",
    source_sidecar_raw: "Mapping[str, object]",
    out_dir: "Path",
) -> None:
    """Serialize the working set to run.duckdb + a regenerated base.json.

    Writes each WorkingTable from its Arrow table into a fresh output DuckDB (the input
    emit stays read-only) in source table order, working-schema column order, and
    canonical content row order, then rebuilds base.json's `tables` array from the
    written catalog -- per-table `rows` and per-column `{name, type}` read back from
    what was just written; the table-level `category` / `record_kind` / `property`
    carried from each `WorkingTable.spec` (C1/C3 require them); per-column `references`
    / `history_tracked` / `temporal_class` read from each column's `WorkingTable.spec`
    `ColumnSpec` (joined to the written catalog by post-drift name, so they follow a
    rename and drop with a drop) -- never re-looked-up from the source sidecar by name.
    Every other
    top-level sidecar field -- including `enum_domains` and `record_roles` -- is copied
    verbatim from `source_sidecar_raw`. Regenerating the sidecar from the written
    catalog makes C2 hold by construction; untouched structural columns make C3/C4/C5
    hold; the copied `branches` makes C8 hold. On any failure after output files start
    being written, the partial `run.duckdb` / `base.json` are removed (best-effort)
    before the error propagates, so a retry into the same `out_dir` is not blocked by
    the engine's refuses-to-overwrite guard.

    Args:
        state: The final working set after all operations.
        source_sidecar_raw: The source sidecar's raw mapping (Sidecar.raw), for the
            verbatim top-level fields.
        out_dir: Destination directory (created if absent).

    Raises:
        ExportRuntimeError: Opening or writing the output run.duckdb / base.json fails
            (the writer failure domain, as with write_duckdb -- not the reader's
            RunDatabaseError).
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportRuntimeError(
            f"failed to create output directory {out_dir}: {exc}"
        ) from exc

    written = _write_run_duckdb(state, out_dir)

    try:
        tables_entries = [
            _build_table_entry(table_name, working_table, written[table_name])
            for table_name, working_table in state.tables.items()
        ]
        sidecar = dict(source_sidecar_raw)
        sidecar["tables"] = tables_entries

        base_json_path = out_dir / "base.json"
        try:
            base_json_path.write_text(
                json.dumps(sidecar, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise ExportRuntimeError(
                f"failed to write {base_json_path}: {exc}"
            ) from exc
    except BaseException:
        # The run.duckdb just written is only half an emit without its sidecar;
        # remove it so a retry into the same out_dir is not refused.
        _remove_partial_output(out_dir)
        raise
