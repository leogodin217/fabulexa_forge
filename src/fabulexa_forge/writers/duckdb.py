"""DuckDB writer for the dimensional exporter.

Materializes QuerySpec SQL to a fresh DuckDB file via the Arrow path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    import duckdb as duckdb_mod

    from fabulexa_forge.exporters.query_spec import QuerySpec, TableKeys
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit

from fabulexa_forge._sql import quote_identifier
from fabulexa_forge.errors import ExportRuntimeError
from fabulexa_forge.writers.relation import WrittenRelation, describe_arrow_columns

# Bookkeeping schema version written to _export_meta.
_CURSOR_FORMAT_VERSION = 1


def write_duckdb(
    emit: "Emit",
    queries: dict[str, str],
    output_path: Path,
    keys: "Mapping[str, TableKeys]",
) -> dict[str, WrittenRelation]:
    """Materialize each query into a new DuckDB file, declaring keys.

    Unchanged Arrow materialization path. A table named in `keys` is created
    with explicit column DDL (names/types transcribed from its Arrow
    schema) plus the declared PRIMARY KEY / UNIQUE constraints, then loaded
    by insert; a table absent from `keys` keeps the CREATE TABLE AS path.
    An empty mapping reproduces today's behavior exactly.

    Args:
        emit: The open (read-only) emit; queried via Emit.query_arrow.
        queries: Mapping of output table name -> SELECT SQL.
        output_path: Output .duckdb file path to create.
        keys: Declared keys per table name; tables without declarations are
            simply absent. Names must be a subset of `queries`' names.

    Returns:
        Mapping of every table name -> its written relation (0 rows for an
        empty table).

    Raises:
        ExportRuntimeError: DuckDB creation or table load fails — including
            a declared-constraint violation, reported naming the table.
        ValueError: `keys` names a table absent from `queries`.
    """
    unknown = set(keys) - set(queries)
    if unknown:
        raise ValueError(
            f"write_duckdb: keys names table(s) absent from queries: {sorted(unknown)}"
        )

    import duckdb

    try:
        out_conn = duckdb.connect(str(output_path))
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to open output DuckDB at {output_path}: {exc}"
        ) from exc

    written: dict[str, WrittenRelation] = {}
    try:
        for table_name, sql in queries.items():
            try:
                table_keys = keys.get(table_name)
                if table_keys is not None:
                    written[table_name] = _create_keyed_table_from_arrow(
                        out_conn, emit, table_name, sql, table_keys
                    )
                else:
                    written[table_name] = _create_table_from_arrow(
                        out_conn, emit, table_name, sql
                    )
            except Exception as exc:
                raise ExportRuntimeError(
                    f"failed to write table '{table_name}' to {output_path}: {exc}"
                ) from exc
    finally:
        out_conn.close()

    return written


def _ensure_bookkeeping_tables(conn: "duckdb_mod.DuckDBPyConnection") -> None:
    """Create _export_meta and _export_windows if they don't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _export_meta (
            cursor_format_version INTEGER NOT NULL,
            fingerprint VARCHAR NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _export_windows (
            window_index INTEGER NOT NULL,
            label VARCHAR NOT NULL,
            start_ns BIGINT NOT NULL,
            end_ns BIGINT NOT NULL
        )
        """
    )


def _table_exists(conn: "duckdb_mod.DuckDBPyConnection", table_name: str) -> bool:
    """Return True if *table_name* exists in the warehouse catalog."""
    rows = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()
    return bool(rows and rows[0] > 0)


def _create_table_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
) -> WrittenRelation:
    """Create *table_name* from the Arrow result of *sql* on *emit*."""
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(
        f"CREATE TABLE {quote_identifier(table_name)} AS SELECT * FROM _arrow_src"
    )
    columns = describe_arrow_columns(conn, "_arrow_src")
    conn.unregister("_arrow_src")
    return WrittenRelation(row_count=int(arrow_table.num_rows), columns=columns)


def _quoted_column_list(columns: tuple[str, ...]) -> str:
    """Render a tuple of column names as a comma-separated quoted list."""
    return ", ".join(quote_identifier(column) for column in columns)


def _key_constraint_clauses(keys: "TableKeys") -> list[str]:
    """Render `TableKeys` as CREATE TABLE constraint clauses."""
    clauses = [f"PRIMARY KEY ({_quoted_column_list(keys.primary_key)})"]
    clauses.extend(f"UNIQUE ({_quoted_column_list(cols)})" for cols in keys.unique)
    return clauses


def _create_keyed_table_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
    keys: "TableKeys",
) -> WrittenRelation:
    """Create *table_name* with explicit column DDL plus declared constraints.

    Column names/types are transcribed from the Arrow result's DuckDB
    schema; the row data is then loaded by insert.
    """
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    try:
        columns = describe_arrow_columns(conn, "_arrow_src")
        column_defs = [
            f"{quote_identifier(name)} {column_type}" for name, column_type in columns
        ]
        ddl = ", ".join(column_defs + _key_constraint_clauses(keys))
        conn.execute(f"CREATE TABLE {quote_identifier(table_name)} ({ddl})")
        conn.execute(
            f"INSERT INTO {quote_identifier(table_name)} SELECT * FROM _arrow_src"
        )
    finally:
        conn.unregister("_arrow_src")
    return WrittenRelation(row_count=int(arrow_table.num_rows), columns=columns)


def _append_rows_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
) -> WrittenRelation:
    """Append rows from the Arrow result of *sql* into existing *table_name*."""
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(f"INSERT INTO {quote_identifier(table_name)} SELECT * FROM _arrow_src")
    columns = describe_arrow_columns(conn, "_arrow_src")
    conn.unregister("_arrow_src")
    return WrittenRelation(row_count=int(arrow_table.num_rows), columns=columns)


def _replace_table_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
) -> WrittenRelation:
    """Replace *table_name* contents with the Arrow result of *sql* on *emit*.

    The table must already exist (created on the first window).
    """
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(f"DELETE FROM {quote_identifier(table_name)}")
    conn.execute(f"INSERT INTO {quote_identifier(table_name)} SELECT * FROM _arrow_src")
    columns = describe_arrow_columns(conn, "_arrow_src")
    conn.unregister("_arrow_src")
    return WrittenRelation(row_count=int(arrow_table.num_rows), columns=columns)


def _apply_spec(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    spec: "QuerySpec",
) -> WrittenRelation:
    """Apply one QuerySpec to the warehouse connection within the active transaction.

    The written relation's row_count is rows written this window (for
    'replace', the full snapshot count).
    """
    exists = _table_exists(conn, spec.table_name)

    if not exists or spec.write_mode == "create":
        if spec.keys is not None:
            return _create_keyed_table_from_arrow(
                conn, emit, spec.table_name, spec.sql, spec.keys
            )
        return _create_table_from_arrow(conn, emit, spec.table_name, spec.sql)
    if spec.write_mode == "append":
        return _append_rows_from_arrow(conn, emit, spec.table_name, spec.sql)
    # write_mode == "replace"
    return _replace_table_from_arrow(conn, emit, spec.table_name, spec.sql)


def _install_view(
    conn: "duckdb_mod.DuckDBPyConnection",
    view_name: str,
    view_sql: str,
) -> None:
    """Install (or replace) a view by name with *view_sql* as its SELECT body."""
    conn.execute(f"CREATE OR REPLACE VIEW {quote_identifier(view_name)} AS {view_sql}")


def write_duckdb_window(
    emit: "Emit",
    specs: "list[QuerySpec]",
    output_path: Path,
    window: "Window",
    fingerprint: str | None,
) -> dict[str, WrittenRelation]:
    """Apply one window to the warehouse file in a single transaction.

    Create-if-missing: a fresh file gets each table created per its spec,
    every view installed, and _export_meta written (cursor_format_version,
    fingerprint). Every invocation, in one transaction: append/replace each
    spec per write_mode, CREATE OR REPLACE each view, insert the window's
    _export_windows row (window_index, label, start_ns, end_ns). Commit or
    roll back atomically — a failed window leaves the warehouse exactly as
    before.

    fingerprint=None is the explicit-range path: the file is a pure
    rendering — no _export_meta, no _export_windows row (the driver has
    already guaranteed a fresh output_path). _export_windows.window_index
    is therefore always non-NULL.

    Args:
        emit: The open (read-only) emit, queried via Emit.query_arrow.
        specs: Windowed QuerySpecs from build_query_specs.
        output_path: The warehouse .duckdb file.
        window: The window being applied (logged with its label/bounds).
        fingerprint: Stored into _export_meta on creation; verified by the
            driver before this is called. None for an explicit range — no
            bookkeeping tables are written.

    Returns:
        Mapping of every spec's physical table_name -> its written relation
        (rows written this window, and its column types).

    Raises:
        ExportRuntimeError: Connection, write, or commit failure (after
            rollback).
    """
    import duckdb

    try:
        conn = duckdb.connect(str(output_path))
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to open warehouse DuckDB at {output_path}: {exc}"
        ) from exc

    written_relations: dict[str, WrittenRelation] = {}
    try:
        conn.begin()
        try:
            if fingerprint is not None:
                is_fresh = not _table_exists(conn, "_export_meta")
                _ensure_bookkeeping_tables(conn)
                if is_fresh:
                    conn.execute(
                        "INSERT INTO _export_meta VALUES (?, ?)",
                        [_CURSOR_FORMAT_VERSION, fingerprint],
                    )

            for spec in specs:
                written_relations[spec.table_name] = _apply_spec(conn, emit, spec)
                if spec.view_name is not None and spec.view_sql is not None:
                    _install_view(conn, spec.view_name, spec.view_sql)

            if fingerprint is not None:
                conn.execute(
                    "INSERT INTO _export_windows VALUES (?, ?, ?, ?)",
                    [window.index, window.label, window.start_ns, window.end_ns],
                )

        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(exc, ExportRuntimeError):
                raise
            raise ExportRuntimeError(
                f"write_duckdb_window failed for window '{window.label}': {exc}"
            ) from exc

        conn.commit()
    finally:
        conn.close()

    return written_relations
