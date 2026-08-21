"""Emit I/O layer for fabulexa_forge.reader.

Provides open_emit (file-location, JSON-parse, Sidecar.from_raw delegation,
read-only DuckDB open) and the Emit handle (query, close, context manager).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import duckdb as _duckdb
    import pyarrow as _pyarrow

    from fabulexa_forge.anchor import EffectiveAnchor

from fabulexa_forge._sql import register_render_functions
from fabulexa_forge.reader.errors import (
    EmitNotFoundError,
    RunDatabaseError,
    SidecarParseError,
)
from fabulexa_forge.reader.sidecar import Sidecar

_BASE_JSON = "base.json"
_RUN_DUCKDB = "run.duckdb"


def _locate_artifacts(emit_dir: Path) -> tuple[Path, Path]:
    """Locate base.json and run.duckdb within emit_dir.

    Args:
        emit_dir: Directory expected to contain the two artifacts.

    Returns:
        A (base_json_path, run_duckdb_path) pair.

    Raises:
        EmitNotFoundError: emit_dir, base.json, or run.duckdb is absent.
    """
    if not emit_dir.exists():
        raise EmitNotFoundError(f"emit directory not found: {emit_dir}")

    base_json_path = emit_dir / _BASE_JSON
    if not base_json_path.exists():
        raise EmitNotFoundError(f"base.json not found in emit directory: {emit_dir}")

    run_duckdb_path = emit_dir / _RUN_DUCKDB
    if not run_duckdb_path.exists():
        raise EmitNotFoundError(f"run.duckdb not found in emit directory: {emit_dir}")

    return base_json_path, run_duckdb_path


def _parse_base_json(base_json_path: Path) -> dict[str, object]:
    """Parse base.json from disk into a raw mapping.

    Args:
        base_json_path: Path to base.json.

    Returns:
        The parsed JSON object as a dict.

    Raises:
        SidecarParseError: base.json is not valid JSON.
    """
    try:
        text = base_json_path.read_text(encoding="utf-8")
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarParseError(f"base.json is not valid JSON: {exc}") from exc
    return cast(dict[str, object], result)


def _open_duckdb_readonly(run_duckdb_path: Path) -> "_duckdb.DuckDBPyConnection":
    """Open run.duckdb in read-only mode.

    Args:
        run_duckdb_path: Path to run.duckdb.

    Returns:
        An open read-only DuckDB connection.

    Raises:
        RunDatabaseError: run.duckdb is present but not a readable DuckDB database.
    """
    import duckdb

    try:
        conn = duckdb.connect(str(run_duckdb_path), read_only=True)
    except Exception as exc:
        raise RunDatabaseError(
            f"run.duckdb could not be opened as a DuckDB database: {exc}"
        ) from exc
    return conn


class Emit:
    """An open base-layer emit: a typed Sidecar plus a read-only DuckDB connection.

    The single sanctioned path to run.duckdb and base.json. A context manager;
    closing releases the connection. Read-only — the reader never mutates the emit.
    """

    def __init__(
        self,
        sidecar: Sidecar,
        emit_dir: Path,
        conn: "_duckdb.DuckDBPyConnection",
    ) -> None:
        self._sidecar = sidecar
        self._emit_dir = emit_dir
        self._conn = conn
        self._closed = False

    @property
    def sidecar(self) -> Sidecar:
        """The typed sidecar for this emit."""
        return self._sidecar

    @property
    def emit_dir(self) -> Path:
        """The directory this emit was opened from."""
        return self._emit_dir

    def query(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[tuple[object, ...]]:
        """Execute a read-only SQL query against run.duckdb and return all rows.

        The sanctioned execution surface over the emit's DuckDB. Values are returned
        exactly as the DuckDB Python client yields them — no type transformation.

        Args:
            sql: A read-only SQL statement (SELECT / catalog introspection).
            parameters: Positional bind parameters; the empty tuple when none.

        Returns:
            All result rows as tuples, in the query's result order.

        Raises:
            RunDatabaseError: The statement fails to execute against run.duckdb.
                The connection is read-only, so a non-read statement (DML/DDL) is
                rejected by DuckDB and surfaces here.
        """
        try:
            result = self._conn.execute(sql, list(parameters))
            rows = result.fetchall()
        except Exception as exc:
            raise RunDatabaseError(f"query failed: {exc}") from exc
        return cast(list[tuple[object, ...]], rows)

    def query_arrow(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> "_pyarrow.Table":
        """Execute a read-only SELECT and return the result as a pyarrow.Table.

        The sanctioned columnar read surface over the emit's DuckDB, alongside
        the row-tuple `query`. Backed by `conn.execute(sql, params).fetch_arrow_table()`
        on the same read-only connection, so the column types are DuckDB's own — a
        zero-row result still carries the typed schema, and a `CAST(NULL AS T)`
        column arrives as a typed all-NULL column (not an untyped object column).

        Args:
            sql: A read-only SELECT against run.duckdb.
            parameters: Positional bind parameters; the empty tuple when none.

        Returns:
            The full result set as a pyarrow.Table, in the query's result order.

        Raises:
            RunDatabaseError: The statement fails to execute against run.duckdb.
        """
        import pyarrow  # noqa: F401 — lazy import, mirrors existing duckdb pattern

        try:
            result = self._conn.execute(sql, list(parameters))
            table = result.fetch_arrow_table()
        except Exception as exc:
            raise RunDatabaseError(f"query_arrow failed: {exc}") from exc
        return cast("_pyarrow.Table", table)

    def close(self) -> None:
        """Close the DuckDB connection. Idempotent."""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "Emit":
        """Return self for `with open_emit(...) as emit:` use."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection on context exit."""
        self.close()


def pin_session_timezone(emit: Emit, anchor: "EffectiveAnchor") -> None:
    """Pin the materialization session's time zone to the anchor zone for
    this invocation.

    Called once by the anchor-resolving driver (the export driver in
    cli.py; tier-2 shaped playback's open) after anchor resolution, before
    any relation materializes. Connection-scoped: covers both reader query
    surfaces (`query` and `query_arrow`). A pure function of the resolved
    anchor — same anchor -> same session state -> byte-identical
    zone-bearing text forms on any machine. Never called by a mode or a
    writer. With no resolved anchor there is no call.

    Args:
        emit: The open emit whose materialization session is pinned.
        anchor: The resolved effective anchor supplying the IANA zone.
    """
    zone = str(anchor.timezone)
    emit._conn.execute(f"SET TimeZone = '{zone}'")


def open_emit(emit_dir: Path) -> Emit:
    """Open a base-layer emit (run.duckdb + base.json) for reading.

    Parses and version-gates base.json, structurally parses it into a typed
    Sidecar, and opens a read-only DuckDB connection over run.duckdb. The returned
    Emit is the sole sanctioned path to both artifacts; no other module opens
    either file.

    Opening performs the version gate and a structural parse only — it does NOT
    run conformance (C1–C11). A sidecar may open successfully and still be
    non-conformant; call `validate` to assess conformance.

    Args:
        emit_dir: Directory containing run.duckdb and base.json. Extra entries are
            ignored — the gate checks the two required artifacts are present, not
            that they are the directory's only contents (an emit may sit inside a
            bundle alongside sibling files).

    Returns:
        An open Emit. The caller closes it via Emit.close() or a `with` block.

    Raises:
        EmitNotFoundError: emit_dir, run.duckdb, or base.json is absent.
        SidecarParseError: base.json is not valid JSON.
        UnsupportedBaseFormatVersionError: base_format_version is a present integer
            other than SUPPORTED_BASE_FORMAT_VERSION; carries that integer as
            found_version. No auto-upgrade.
        SidecarStructureError: base.json is valid JSON but malformed for the
            supported version — base_format_version absent or non-integer, or the
            required top-level structure (branches, tables, or a required field of
            either) absent or mis-typed.
        RunDatabaseError: run.duckdb is present but is not a readable DuckDB
            database.
    """
    base_json_path, run_duckdb_path = _locate_artifacts(emit_dir)
    raw = _parse_base_json(base_json_path)
    sidecar = Sidecar.from_raw(raw)
    conn = _open_duckdb_readonly(run_duckdb_path)
    register_render_functions(conn)
    return Emit(sidecar=sidecar, emit_dir=emit_dir, conn=conn)
