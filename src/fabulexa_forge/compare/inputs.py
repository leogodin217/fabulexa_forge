"""Compare surface input loading: the UTC-pinned session, expected/actual
resolution, and the CSV directory scan + per-family typing casts.

`compare_datasets` (`engine.py`) never opens `run.duckdb` or `base.json` — it
opens its own two comparison inputs through the surface's own in-memory
DuckDB session, zone-pinned to UTC before either input is read. This module
owns that session and both input resolutions: the expected side (a DuckDB
file, `ATTACH`ed as `expected_db`) and the actual side (a DuckDB file
`ATTACH`ed as `actual_db`, or a directory of CSV files registered as
all-text relations in the session). It also owns the one SQL-side typing
step — casting an actual-side CSV cell's raw text toward the expected
column's canonical-family reference type — including the two bespoke
parses (blob hex-decode, interval writer-form-then-`TRY_CAST`) the design
doc pins.

See `docs/architecture/pending/dataset-equivalence.md` § Inputs and the
schema authority, § Canonical value encoding for the semantic authority.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import duckdb as _duckdb
    import pyarrow as _pyarrow

from fabulexa_forge._sql import _sql_literal, quote_identifier
from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value
from fabulexa_forge.compare.errors import CompareInputError

_MAIN_SCHEMA = "main"

_REFERENCE_TYPE: dict[CanonicalFamily, str] = {
    "integer": "BIGINT",
    "float": "DOUBLE",
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    "timestamptz": "TIMESTAMPTZ",
    "interval": "INTERVAL",
}

_INTERVAL_WRITER_FORM = re.compile(r"^(-)?(\d+):([0-5]\d):([0-5]\d)\.(\d{6})$")


@dataclass(frozen=True)
class DuckDbSide:
    """A DuckDB-backed compare side: its `ATTACH` alias in the compare session."""

    alias: str


@dataclass(frozen=True)
class CsvTable:
    """One CSV file registered into the compare session as an all-text relation."""

    view_name: str
    columns: tuple[str, ...]  # header column names, file order


@dataclass(frozen=True)
class CsvSide:
    """A CSV-directory-backed compare side: one `CsvTable` per discovered file."""

    tables: dict[str, CsvTable]


ActualSide = DuckDbSide | CsvSide


def open_compare_session() -> "_duckdb.DuckDBPyConnection":
    """Open the compare surface's own in-memory DuckDB session.

    Zone-pinned to UTC (`SET TimeZone`) before either input is read — the
    compare-side analogue of the reader's session-zone pin, discharging the
    same machine-independence obligation for offset-less timestamptz CSV
    text. A fixed constant of the surface, not a resolved value: compare has
    no emit and no anchor to pin to.

    Returns:
        A fresh in-memory DuckDB connection with `TimeZone` set to `'UTC'`.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone = 'UTC'")
    return conn


def attach_expected(conn: "_duckdb.DuckDBPyConnection", path: "Path") -> None:
    """Attach the expected side as `expected_db`, read-only.

    Args:
        conn: The compare session.
        path: The claimed expected-side DuckDB file.

    Raises:
        CompareInputError: `path` does not open as a DuckDB database file.
    """
    try:
        conn.execute(f"ATTACH {_sql_literal(str(path))} AS expected_db (READ_ONLY)")
    except Exception as exc:
        raise CompareInputError(f"expected side must be a DuckDB file: {path}") from exc


def resolve_actual(conn: "_duckdb.DuckDBPyConnection", path: "Path") -> ActualSide:
    """Resolve and open the actual side: a DuckDB file, or a CSV directory.

    Args:
        conn: The compare session.
        path: The claimed actual-side path.

    Returns:
        A `DuckDbSide` (attached as `actual_db`) or a `CsvSide` (one
        `CsvTable` registered per top-level `*.csv` file).

    Raises:
        CompareInputError: `path` is neither a DuckDB file nor a directory
            containing at least one `.csv` file; or a discovered CSV file
            has no header row.
    """
    if path.is_dir():
        csv_files = _discover_csv_files(path)
        if not csv_files:
            raise CompareInputError(
                f"actual side is neither a DuckDB file nor a CSV directory: {path}"
            )
        return CsvSide(tables={f.stem: _register_csv_table(conn, f) for f in csv_files})
    try:
        conn.execute(f"ATTACH {_sql_literal(str(path))} AS actual_db (READ_ONLY)")
    except Exception as exc:
        raise CompareInputError(
            f"actual side is neither a DuckDB file nor a CSV directory: {path}"
        ) from exc
    return DuckDbSide("actual_db")


def _discover_csv_files(directory: "Path") -> tuple["Path", ...]:
    """Top-level `*.csv` files only (case-sensitive extension); subdirectories
    and non-`.csv` entries are ignored.
    """
    return tuple(
        sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".csv")
    )


def _register_csv_table(conn: "_duckdb.DuckDBPyConnection", path: "Path") -> CsvTable:
    """Parse one CSV file and register it in the session as an all-text relation.

    Args:
        conn: The compare session.
        path: The CSV file; its stem is the table name.

    Returns:
        A `CsvTable` naming the registered view and its header columns.

    Raises:
        CompareInputError: The file has no header row.
    """
    text = path.read_text(encoding="utf-8")
    rows = _tokenize_csv(text)
    if not rows:
        raise CompareInputError(f"CSV file has no header row: {path}")
    header = [cell if cell is not None else "" for cell in rows[0]]
    view_name = f"csv_{uuid.uuid4().hex}"
    conn.register(view_name, _rows_to_arrow(header, rows[1:]))
    return CsvTable(view_name=view_name, columns=tuple(header))


def _tokenize_csv(text: str) -> list[list[str | None]]:
    """Parse RFC4180 CSV text into rows of fields.

    Distinguishes an unquoted empty field (`None`, i.e. NULL) from a quoted
    empty field (`""`, i.e. the empty string) — the one place CSV must
    retain what DuckDB storage distinguishes natively. Handles doubled-quote
    escaping and embedded delimiters/newlines within quoted fields.

    Args:
        text: The full CSV file content.

    Returns:
        One list of fields per row (header included, as the first row); an
        empty list for empty input.
    """
    rows: list[list[str | None]] = []
    row: list[str | None] = []
    buf: list[str] = []
    quoted = False
    in_quotes = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not buf:
            quoted = True
            in_quotes = True
            i += 1
            continue
        if ch == ",":
            row.append("".join(buf) if (quoted or buf) else None)
            buf = []
            quoted = False
            i += 1
            continue
        if ch == "\r":
            i += 1
            continue
        if ch == "\n":
            row.append("".join(buf) if (quoted or buf) else None)
            rows.append(row)
            row = []
            buf = []
            quoted = False
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf or quoted or row:
        row.append("".join(buf) if (quoted or buf) else None)
        rows.append(row)
    return rows


def _rows_to_arrow(
    header: "Sequence[str]", rows: "Sequence[Sequence[str | None]]"
) -> "_pyarrow.Table":
    """Assemble parsed CSV data rows into an all-text pyarrow Table.

    Args:
        header: Column names, file order.
        rows: Data rows (header excluded), each field already resolved to
            `None` (NULL) or `str` (including the empty string).

    Returns:
        A `pyarrow.Table` with one `pyarrow.string()` column per header name.
    """
    import pyarrow as pa

    columns: dict[str, list[str | None]] = {name: [] for name in header}
    for row in rows:
        for name, value in zip(header, row):
            columns[name].append(value)
    return cast(
        "_pyarrow.Table",
        pa.table(
            {
                name: pa.array(values, type=pa.string())
                for name, values in columns.items()
            }
        ),
    )


def list_tables(conn: "_duckdb.DuckDBPyConnection", alias: str) -> tuple[str, ...]:
    """List `main`-schema table names for an attached DuckDB alias, sorted.

    Args:
        conn: The compare session.
        alias: The `ATTACH` alias (`expected_db` or `actual_db`).

    Returns:
        Sorted table names visible in the alias's `main` schema.
    """
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = ? AND schema_name = ? ORDER BY table_name",
        [alias, _MAIN_SCHEMA],
    ).fetchall()
    return tuple(cast(str, r[0]) for r in rows)


def list_columns(
    conn: "_duckdb.DuckDBPyConnection", alias: str, table: str
) -> tuple[tuple[str, str], ...]:
    """List `(column_name, duckdb_type)` for one `main`-schema table.

    Args:
        conn: The compare session.
        alias: The `ATTACH` alias (`expected_db` or `actual_db`).
        table: The table name within the alias's `main` schema.

    Returns:
        `(name, duckdb_type)` pairs in catalog declaration order.
    """
    rows = conn.execute(
        "SELECT column_name, data_type FROM duckdb_columns() "
        "WHERE database_name = ? AND schema_name = ? AND table_name = ? "
        "ORDER BY column_index",
        [alias, _MAIN_SCHEMA, table],
    ).fetchall()
    return tuple((cast(str, r[0]), cast(str, r[1])) for r in rows)


class _RawInterval(NamedTuple):
    """A month/day/nanosecond triple, duck-typed to `encode_value`'s interval
    branch (which reads `.months` / `.days` / `.nanoseconds`)."""

    months: int
    days: int
    nanoseconds: int


def _parse_interval_writer_form(text: str) -> _RawInterval | None:
    """Parse the pinned `[-]H:MM:SS.ffffff` writer form directly.

    Args:
        text: The raw CSV cell text.

    Returns:
        The parsed pure-microsecond interval, or None if `text` does not
        match the pinned form (the caller falls back to `TRY_CAST`).
    """
    match = _INTERVAL_WRITER_FORM.match(text)
    if match is None:
        return None
    sign = -1 if match.group(1) else 1
    hours, minutes, seconds, micros = (int(match.group(g)) for g in (2, 3, 4, 5))
    total_us = (hours * 3600 + minutes * 60 + seconds) * 1_000_000 + micros
    return _RawInterval(months=0, days=0, nanoseconds=sign * total_us * 1000)


def _resolve_blob_cell(raw: str) -> str:
    """Resolve one raw CSV blob cell: lowercase-hex decode, or the raw text
    on a decode failure (a value discrepancy, never an error)."""
    try:
        decoded = bytes.fromhex(raw)
    except ValueError:
        return raw
    result = encode_value(decoded, "blob")
    assert result is not None
    return result


def _resolve_interval_cell(raw: str, cast_value: object) -> str:
    """Resolve one raw CSV interval cell: the pinned writer form parsed
    directly, else the `TRY_CAST` fallback result, else the raw text on a
    cast failure (a value discrepancy, never an error)."""
    bespoke = _parse_interval_writer_form(raw)
    if bespoke is not None:
        result = encode_value(bespoke, "interval")
        assert result is not None
        return result
    if cast_value is None:
        return raw
    result = encode_value(cast_value, "interval")
    assert result is not None
    return result


def _resolve_csv_cell(
    raw: str | None, cast_value: object, family: CanonicalFamily
) -> str | None:
    """Combine one CSV cell's raw text and (if applicable) its `TRY_CAST`
    result into the final canonical-comparison string.

    Args:
        raw: The raw CSV text, or None for an unquoted-empty (NULL) field.
        cast_value: The materialized `TRY_CAST(raw AS <reference type>)`
            result (irrelevant for the text and blob families).
        family: The expected column's canonical family.

    Returns:
        None for NULL; the raw text unchanged for the text family; the
        canonical encoding of a successful cast; the raw text itself for a
        failed cast (a value discrepancy, never an error).
    """
    if raw is None:
        return None
    if family == "text":
        return raw
    if family == "blob":
        return _resolve_blob_cell(raw)
    if family == "interval":
        return _resolve_interval_cell(raw, cast_value)
    if cast_value is None:
        return raw
    result = encode_value(cast_value, family)
    assert result is not None
    return result


def _select_fragment(name: str, family: CanonicalFamily) -> tuple[str, bool]:
    """Build one compared column's `SELECT` fragment for a CSV table.

    Args:
        name: The column name (a CSV header, matching the expected side).
        family: The expected column's canonical family.

    Returns:
        `(fragment, has_cast_column)` — the fragment always projects the raw
        text as `<name>__raw`; `has_cast_column` is True when it also
        projects `TRY_CAST(... AS <reference type>) AS <name>__cast`
        (every family but text and blob, which resolve without a SQL cast).
    """
    ident = quote_identifier(name)
    raw_alias = quote_identifier(f"{name}__raw")
    if family in ("text", "blob"):
        return f"{ident} AS {raw_alias}", False
    ref_type = _REFERENCE_TYPE[family]
    cast_alias = quote_identifier(f"{name}__cast")
    return (
        f"{ident} AS {raw_alias}, TRY_CAST({ident} AS {ref_type}) AS {cast_alias}",
        True,
    )


def csv_column_values(
    conn: "_duckdb.DuckDBPyConnection",
    table: CsvTable,
    columns: "Sequence[tuple[str, CanonicalFamily]]",
) -> list[tuple[str | None, ...]]:
    """Materialize a CSV table's compared columns, resolved to their final
    canonical-comparison strings.

    Args:
        conn: The compare session.
        table: The registered CSV table.
        columns: The compared columns to project, in the order to return
            them, each paired with the expected side's canonical family.

    Returns:
        One tuple per row (row-major), each entry the resolved comparison
        string, or None for NULL, in `columns` order. Requires `columns`
        non-empty.
    """
    fragments: list[str] = []
    has_cast: list[bool] = []
    for name, family in columns:
        fragment, cast_present = _select_fragment(name, family)
        fragments.append(fragment)
        has_cast.append(cast_present)
    sql = f"SELECT {', '.join(fragments)} FROM {table.view_name}"
    arrow_table = conn.execute(sql).fetch_arrow_table()
    raw_cols = [arrow_table.column(f"{name}__raw") for name, _ in columns]
    cast_cols = [
        arrow_table.column(f"{name}__cast") if present else None
        for (name, _), present in zip(columns, has_cast)
    ]
    rows: list[tuple[str | None, ...]] = []
    for i in range(arrow_table.num_rows):
        row: list[str | None] = []
        for j, (_, family) in enumerate(columns):
            raw = raw_cols[j][i].as_py()
            cast_col = cast_cols[j]
            cast_value = cast_col[i].as_py() if cast_col is not None else None
            row.append(_resolve_csv_cell(raw, cast_value, family))
        rows.append(tuple(row))
    return rows
