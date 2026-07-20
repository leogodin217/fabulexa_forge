"""Mode-neutral compiled-table representation shared by every exporter engine.

`QuerySpec` used to live in the dimensional engine; it is relocated here so a
second mode (source) can compile to the same writer-ready shape without
importing across mode boundaries (exporters.source must never import
exporters.dimensional, or vice versa). `write_query_specs` is the shared
full-export write dispatch every mode's `export_*` entry point calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.reader.emit import Emit


@dataclass(frozen=True)
class QuerySpec:
    """A compiled output table: name, SELECT, write mode, optional view pair.

    Full export compiles every table with write_mode='create' and no view —
    the existing shape. A windowed compile tags facts and SCD-2 version
    tables 'append', type-1 dims 'replace', and carries the companion view
    (name + DDL SELECT body) for SCD-2 dims that declare a valid_to column.
    """

    table_name: str
    sql: str
    write_mode: Literal["create", "append", "replace"]
    view_name: str | None
    view_sql: str | None


def query_spec_output_name(spec: QuerySpec) -> str:
    """The spec's author-facing output-table name.

    An SCD-2 dim windowed with a `valid_to` column compiles to a physical
    `<name>__rows` spec plus a companion view named the author's declared
    table name (`view_name`); every other spec carries no view, so its
    `table_name` already is the author name. Shared by the incremental
    driver's CSV writer and tier-2 `ShapedPlayback.window()` so the two
    surfaces name a windowed SCD-2 dim's output identically.

    Args:
        spec: A compiled QuerySpec.

    Returns:
        `spec.view_name` when present, else `spec.table_name`.
    """
    return spec.view_name if spec.view_name is not None else spec.table_name


def write_query_specs(
    emit: "Emit",
    specs: list[QuerySpec],
    out: "Path",
    fmt: Literal["csv", "duckdb"],
) -> dict[str, int]:
    """Dispatch a full-export QuerySpec list to the writer selected by fmt.

    Every mode's full-export path (dimensional, source) compiles to this one
    shape and shares this dispatch: flattens to name -> SQL and hands off to
    `writers.duckdb.write_duckdb`, or one `writers.csv.write_csv` call per
    table.

    Args:
        emit: The open emit.
        specs: The compiled QuerySpecs (each write_mode='create').
        out: The output target — a directory receiving one `<table>.csv` per
            table (fmt='csv'), or the `.duckdb` file path to create
            (fmt='duckdb').
        fmt: Output format.

    Returns:
        Mapping of every table name -> row count written (0 for a table whose
        query resolved to no rows; such a table is still emitted — empty
        typed DuckDB table or header-only CSV — never dropped).

    Raises:
        ExportRuntimeError: A writer fails.
    """
    queries = {spec.table_name: spec.sql for spec in specs}

    if fmt == "duckdb":
        from fabulexa_forge.writers.duckdb import write_duckdb

        return write_duckdb(emit, queries, out)

    from fabulexa_forge.writers.csv import write_csv

    row_counts: dict[str, int] = {}
    for table_name, sql in queries.items():
        row_counts[table_name] = write_csv(emit, table_name, sql, out)
    return row_counts
