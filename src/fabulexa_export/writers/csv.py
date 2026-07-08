"""CSV writer for the dimensional exporter.

Materializes QuerySpec SQL to a CSV file in a directory via the Arrow path.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fabulexa_export.reader.emit import Emit

from fabulexa_export.errors import ExportRuntimeError


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
    arrow_table = emit.query_arrow(query, ())
    out_path = output_dir / f"{table_name}.csv"

    try:
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


def _format_value(scalar: object) -> object:
    """Convert a pyarrow scalar to a Python value suitable for CSV writing.

    Args:
        scalar: A pyarrow scalar value from an Arrow column slice.

    Returns:
        The Python representation of the scalar (None for null).
    """
    import pyarrow as pa

    if isinstance(scalar, pa.Scalar):
        if scalar.is_valid:
            return scalar.as_py()
        return None
    return scalar
