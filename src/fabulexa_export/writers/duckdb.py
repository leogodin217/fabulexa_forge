"""DuckDB writer for the dimensional exporter.

Materializes QuerySpec SQL to a fresh DuckDB file via the Arrow path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb as duckdb_mod

    from fabulexa_export.exporters.query_spec import QuerySpec
    from fabulexa_export.incremental.windows import Window
    from fabulexa_export.reader.emit import Emit

from fabulexa_export.errors import ExportRuntimeError

# Bookkeeping schema version written to _export_meta.
_CURSOR_FORMAT_VERSION = 1


def write_duckdb(
    emit: "Emit",
    queries: dict[str, str],
    output_path: Path,
) -> dict[str, int]:
    """Materialize each query and write it to a new DuckDB file (Arrow path).

    For each query, `emit.query_arrow(sql, ())` yields a typed Arrow table; the
    writer registers it on a **fresh output** connection (`duckdb.connect(output_path)`
    — the writer owns this output DB; the input emit stays read-only and
    untouched) and runs CREATE TABLE AS. The Arrow path is deliberate: it
    sidesteps the all-null-object-column register failure (NULL-pad columns
    arrive already CAST to VARCHAR, so even an all-NULL column is typed, not an
    untyped object column). A zero-row result still carries its typed Arrow
    schema, so an empty grain yields an **empty typed table**, never a dropped
    one.

    Args:
        emit: The open (read-only) emit; queried via Emit.query_arrow.
        queries: Mapping of output table name -> SELECT SQL.
        output_path: Output .duckdb file path to create.

    Returns:
        Mapping of every table name -> row count (0 for an empty table).

    Raises:
        ExportRuntimeError: DuckDB creation or table copy fails.
    """
    import duckdb

    try:
        out_conn = duckdb.connect(str(output_path))
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to open output DuckDB at {output_path}: {exc}"
        ) from exc

    row_counts: dict[str, int] = {}
    try:
        for table_name, sql in queries.items():
            arrow_table = emit.query_arrow(sql, ())
            try:
                out_conn.register("_arrow_src", arrow_table)
                out_conn.execute(
                    f'CREATE TABLE "{table_name}" AS SELECT * FROM _arrow_src'
                )
                out_conn.unregister("_arrow_src")
            except Exception as exc:
                raise ExportRuntimeError(
                    f"failed to write table '{table_name}' to {output_path}: {exc}"
                ) from exc
            row_counts[table_name] = arrow_table.num_rows
    finally:
        out_conn.close()

    return row_counts


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
) -> int:
    """Create *table_name* from the Arrow result of *sql* on *emit*.

    Returns the number of rows written.
    """
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(f'CREATE TABLE "{table_name}" AS SELECT * FROM _arrow_src')
    conn.unregister("_arrow_src")
    return int(arrow_table.num_rows)


def _append_rows_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
) -> int:
    """Append rows from the Arrow result of *sql* into existing *table_name*.

    Returns the number of rows appended.
    """
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(f'INSERT INTO "{table_name}" SELECT * FROM _arrow_src')
    conn.unregister("_arrow_src")
    return int(arrow_table.num_rows)


def _replace_table_from_arrow(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    table_name: str,
    sql: str,
) -> int:
    """Replace *table_name* contents with the Arrow result of *sql* on *emit*.

    The table must already exist (created on the first window). Returns the
    number of rows in the replacement snapshot.
    """
    arrow_table = emit.query_arrow(sql, ())
    conn.register("_arrow_src", arrow_table)
    conn.execute(f'DELETE FROM "{table_name}"')
    conn.execute(f'INSERT INTO "{table_name}" SELECT * FROM _arrow_src')
    conn.unregister("_arrow_src")
    return int(arrow_table.num_rows)


def _apply_spec(
    conn: "duckdb_mod.DuckDBPyConnection",
    emit: "Emit",
    spec: "QuerySpec",
) -> int:
    """Apply one QuerySpec to the warehouse connection within the active transaction.

    Returns rows written this window (for 'replace', the full snapshot count).
    """
    exists = _table_exists(conn, spec.table_name)

    if not exists or spec.write_mode == "create":
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
    conn.execute(f'CREATE OR REPLACE VIEW "{view_name}" AS {view_sql}')


def write_duckdb_window(
    emit: "Emit",
    specs: "list[QuerySpec]",
    output_path: Path,
    window: "Window",
    fingerprint: str | None,
) -> dict[str, int]:
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
        Mapping of every table name -> rows written this window.

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

    row_counts: dict[str, int] = {}
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
                rows = _apply_spec(conn, emit, spec)
                row_counts[spec.table_name] = rows
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

    return row_counts
