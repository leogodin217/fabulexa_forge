"""Mode-neutral compiled-table representation shared by every exporter engine.

`QuerySpec` used to live in the dimensional engine; it is relocated here so a
second mode (source) can compile to the same writer-ready shape without
importing across mode boundaries (exporters.source must never import
exporters.dimensional, or vice versa). `write_query_specs` is the shared
full-export write dispatch every mode's `export_*` entry point calls.
`keys_not_declarable_csv_notice` is the shared notice the base and source
full-export entry paths, and the incremental driver, all emit identically
when `declare_keys` meets a CSV target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.exporters.notices import Notice

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.reader.emit import Emit


@dataclass(frozen=True)
class TableKeys:
    """Declared key metadata for one compiled output table.

    Column names are post-`rename` output names. Carried by `QuerySpec`;
    materialized as constraints by the DuckDB writer, reported as
    undeliverable by the CSV dispatch.

    A table with nothing to declare carries `QuerySpec.keys = None`, never
    an empty `TableKeys`: every constructed instance has a non-empty
    `primary_key` (the resolution table always yields one), while `unique`
    may be empty (no block claim → identity keys only).
    """

    primary_key: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TableReport:
    """One output table as written.

    `columns` are (output name, type-text) pairs in output order, transcribed
    from the materialized relation via the writers' DESCRIBE authority.
    `row_count` is None on windowed invocations. `keys` is the table's
    declared `TableKeys`, or None when nothing was declared or the
    declaration was CSV-dropped.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    row_count: int | None
    keys: TableKeys | None


@dataclass(frozen=True)
class ExportReport:
    """Per-table reports for one export invocation, in plan iteration order.

    Returned by every file-writing export entry point in place of the bare
    table -> row-count mapping.
    """

    tables: tuple[TableReport, ...]


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
    keys: TableKeys | None = None


NOTICE_KEYS_NOT_DECLARABLE_CSV = "keys-not-declarable-csv"
"""The notice code 'keys-not-declarable-csv'."""


def keys_not_declarable_csv_notice() -> Notice:
    """The one notice a declare_keys-under-CSV invocation emits.

    Shared by the base and source full-export entry paths and the incremental
    driver so all three emit an identical, deterministic message: CSV carries
    no constraint surface, the data is unchanged, and the declaration is
    dropped for this invocation.

    Returns:
        A Notice with code NOTICE_KEYS_NOT_DECLARABLE_CSV and a fully
        rendered, self-contained message.
    """
    return Notice(
        code=NOTICE_KEYS_NOT_DECLARABLE_CSV,
        message=(
            "declare_keys is on, but CSV carries no constraint surface: the"
            " data is unchanged, and the key declaration is dropped for this"
            " invocation"
        ),
    )


def declare_keys_active(config: "ExportConfig") -> bool:
    """Whether the config's mode section has `declare_keys` on.

    Dispatches on `config.mode` to the matching section — dimensional carries
    no `declare_keys` field, so it is always off. Absent section or absent/
    False `declare_keys` is off: `declare_keys` is never invented, only read
    from config. Shared by the base engine, source engine, and incremental
    driver so all three read the same semantic-default posture.

    Args:
        config: The validated export config.

    Returns:
        True iff the mode-matching section is present and declare_keys is True.
    """
    if config.mode == "base":
        return config.base is not None and config.base.declare_keys is True
    if config.mode == "source":
        return config.source is not None and config.source.declare_keys is True
    return False


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
) -> ExportReport:
    """Dispatch a full-export QuerySpec list to the writer selected by fmt.

    Every mode's full-export path (dimensional, source, base) compiles to
    this one shape and shares this dispatch: flattens to name -> SQL and
    hands off to `writers.duckdb.write_duckdb`, or one `writers.csv.write_csv`
    call per table. Every full-export spec is `write_mode='create'` with no
    view, so a spec's `table_name` is already its author-facing output name.

    Args:
        emit: The open emit.
        specs: The compiled QuerySpecs (each write_mode='create').
        out: The output target — a directory receiving one `<table>.csv` per
            table (fmt='csv'), or the `.duckdb` file path to create
            (fmt='duckdb').
        fmt: Output format.

    Returns:
        One `TableReport` per spec, in plan iteration order — row count and
        columns from the writer's `WrittenRelation`, `keys` the spec's
        declared keys under `fmt='duckdb'`, always None under `fmt='csv'`
        (CSV carries no constraint surface). A table whose query resolved to
        no rows is still reported — empty typed DuckDB table or header-only
        CSV — never dropped.

    Raises:
        ExportRuntimeError: A writer fails.
    """
    queries = {spec.table_name: spec.sql for spec in specs}

    if fmt == "duckdb":
        from fabulexa_forge.writers.duckdb import write_duckdb

        keys = {spec.table_name: spec.keys for spec in specs if spec.keys is not None}
        written = write_duckdb(emit, queries, out, keys)
        return ExportReport(
            tables=tuple(
                TableReport(
                    name=spec.table_name,
                    columns=written[spec.table_name].columns,
                    row_count=written[spec.table_name].row_count,
                    keys=spec.keys,
                )
                for spec in specs
            )
        )

    from fabulexa_forge.writers.csv import write_csv

    tables: list[TableReport] = []
    for spec in specs:
        relation = write_csv(emit, spec.table_name, spec.sql, out)
        tables.append(
            TableReport(
                name=spec.table_name,
                columns=relation.columns,
                row_count=relation.row_count,
                keys=None,
            )
        )
    return ExportReport(tables=tuple(tables))
