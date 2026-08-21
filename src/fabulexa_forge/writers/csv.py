"""CSV writer for the dimensional exporter.

Materializes QuerySpec SQL to a CSV file in a directory via the Arrow path.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import datetime as _datetime
    from decimal import Decimal

    import pyarrow as pa

    from fabulexa_forge.reader.emit import Emit

from fabulexa_forge.errors import ExportRuntimeError


def write_csv(
    emit: "Emit",
    table_name: str,
    query: str,
    output_dir: Path,
) -> int:
    """Materialize one query and write it as a CSV file with a header row.

    `emit.query_arrow(query, ())` yields a typed Arrow table rendered to
    `output_dir / f"{table_name}.csv"`. A zero-row result writes a **header-only
    file** (the Arrow schema supplies the header), mirroring the DuckDB writer's
    empty-typed-table rule — the declared table is always present.

    Args:
        emit: The open (read-only) emit; queried via Emit.query_arrow.
        table_name: Output filename stem (table_name.csv).
        query: SELECT SQL.
        output_dir: Directory for the CSV.

    Returns:
        Row count written (0 for a header-only file).

    Raises:
        ExportRuntimeError: Query execution or file write fails.
    """
    out_path = output_dir / f"{table_name}.csv"

    try:
        arrow_table = emit.query_arrow(query, ())
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(arrow_table.schema.names)
        for batch in arrow_table.to_batches():
            for row_idx in range(batch.num_rows):
                writer.writerow(
                    _format_value(batch.column(col_idx)[row_idx])
                    for col_idx in range(batch.num_columns)
                )
        out_path.write_text(buf.getvalue(), encoding="utf-8")
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to write CSV for table '{table_name}' to {out_path}: {exc}"
        ) from exc

    return cast(int, arrow_table.num_rows)


def _format_date(value: "_datetime.date") -> str:
    """Render a DATE value in the pinned CSV text form.

    Args:
        value: The materialized date.

    Returns:
        `YYYY-MM-DD`.
    """
    return value.isoformat()


def _format_time(value: "_datetime.time") -> str:
    """Render a TIME value in the pinned CSV text form.

    Args:
        value: The materialized time-of-day.

    Returns:
        `HH:MM:SS.ffffff` — fixed six-digit microsecond field.
    """
    return value.strftime("%H:%M:%S.%f")


def _format_timestamptz(value: "_datetime.datetime") -> str:
    """Render a TIMESTAMPTZ value in the pinned CSV text form.

    The value already carries the pinned session zone as its `tzinfo` (the
    session-zone pin, § Serialization) — `value`'s wall-clock fields are
    already local to the anchor zone; only the offset needs formatting.

    Args:
        value: The materialized zone-aware instant.

    Returns:
        `YYYY-MM-DD HH:MM:SS.ffffff±HH:MM` — local wall clock in the
        value-attached zone, that instant's UTC offset, fixed six-digit
        microsecond field.
    """
    offset = value.utcoffset()
    assert offset is not None, "a TIMESTAMPTZ value always carries an offset"
    sign = "-" if offset.total_seconds() < 0 else "+"
    offset_minutes = int(abs(offset).total_seconds()) // 60
    offset_hours, offset_minutes = divmod(offset_minutes, 60)
    wall_clock = value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return f"{wall_clock}{sign}{offset_hours:02d}:{offset_minutes:02d}"


def _format_interval(value: "pa.MonthDayNano") -> str:
    """Render an INTERVAL value in the pinned CSV text form.

    Assumes the mode-definitional shape of every INTERVAL this codebase
    renders (`build_elapsed_expr`'s `interval` branch): a pure microsecond
    duration with no month/day calendar components.

    Args:
        value: The materialized month/day/nanosecond interval.

    Returns:
        The signed microsecond delta as `[-]H:MM:SS.ffffff` — unbounded
        hours, fixed six-digit microsecond field, no calendar components.
    """
    sign = "-" if value.nanoseconds < 0 else ""
    total_us = abs(value.nanoseconds) // 1000
    total_seconds, microseconds = divmod(total_us, 1_000_000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}.{microseconds:06d}"


def _format_decimal(value: "Decimal") -> str:
    """Render a DECIMAL(p, s) value in the pinned CSV text form.

    Args:
        value: The materialized decimal, already scaled to the column's
            declared `s` by Arrow (§ decimal election).

    Returns:
        Plain fixed-point decimal text — never exponent notation.
    """
    return format(value, "f")


def _format_value(scalar: object) -> object:
    """Convert a pyarrow scalar to a Python value suitable for CSV writing.

    DATE / TIME / TIMESTAMPTZ / INTERVAL / DECIMAL format by the pinned
    per-type text forms (§ Serialization); every other type falls through
    to its existing `.as_py()` representation, byte-identical to before
    this grew the five new forms.

    Args:
        scalar: A pyarrow scalar value from an Arrow column slice.

    Returns:
        The Python representation of the scalar (None for null).
    """
    import pyarrow as pa

    if not isinstance(scalar, pa.Scalar):
        return scalar
    if not scalar.is_valid:
        return None

    value = scalar.as_py()
    if pa.types.is_date(scalar.type):
        return _format_date(value)
    if pa.types.is_time(scalar.type):
        return _format_time(value)
    if pa.types.is_timestamp(scalar.type) and scalar.type.tz is not None:
        return _format_timestamptz(value)
    if pa.types.is_interval(scalar.type):
        return _format_interval(value)
    if pa.types.is_decimal(scalar.type):
        return _format_decimal(value)
    return value
