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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from fabulexa_forge.exporters.notices import Notice

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
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
class ColumnProvenance:
    """The one source column that faithfully fed an output column.

    Stamped at plan compile for columns whose value is the faithful carry
    of exactly one source (table, column). Computed and multi-source
    columns get no entry — absence is the "inherits nothing" answer.

    `value_map` is set only for a `derived: value_map` column: the applied
    substitution as (source value, rendered output text) pairs in the map's
    declaration order. The dictionary resolvers translate the source
    property's declared enum values through it so the documented domain is
    the values the column actually renders; None means the carry is
    value-identical.
    """

    source_table: str
    source_column: str
    value_map: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True)
class KindValueEntry:
    """One rendered label of a kind-name-as-value output column.

    label is the post-`kind_labels` rendered value; source_kind names the
    kind whose rows render under it. List order is the plan's event-source
    compile order.
    """

    label: str
    source_kind: str


def build_carried_provenance(
    source_table: str,
    columns: "Iterable[tuple[str, str]]",
) -> "dict[str, ColumnProvenance]":
    """Stamp provenance for a set of straight (source, output) column carries.

    Shared by the source and base plan builders: every output column of a
    `state` / `junction` table or a base flat table is a faithful, single-
    source carry — projection, rename, or cast-back — of one source column
    on one source table (no `lookup` / `derived` mode, unlike dimensional),
    so one uniform map suffices for all three.

    Args:
        source_table: The one source table every entry is keyed against.
        columns: (source column, output column) pairs — the caller's final,
            post-selection/rename column set. A caller excludes any column
            whose value is not a faithful single-source carry (e.g. a base
            table's re-derived `<kind>_key` / `<p>_key` edge keys) before
            calling this.

    Returns:
        Output column name -> ColumnProvenance, one entry per pair.
    """
    return {
        out: ColumnProvenance(source_table=source_table, source_column=src)
        for src, out in columns
    }


@dataclass(frozen=True)
class TableReport:
    """One output table as written.

    `columns` are (output name, type-text) pairs in output order, transcribed
    from the materialized relation via the writers' DESCRIBE authority.
    `row_count` is None on windowed invocations. `keys` is the table's
    declared `TableKeys`, or None when nothing was declared or the
    declaration was CSV-dropped. `provenance` and `kind_values` are forwarded
    verbatim from the compiled `QuerySpec` that produced this table — no
    default, so every report-assembly call site states them explicitly.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    row_count: int | None
    keys: TableKeys | None
    provenance: "Mapping[str, ColumnProvenance]"
    kind_values: "Mapping[str, tuple[KindValueEntry, ...]]"


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

    `provenance` and `kind_values` are keyed by output column name
    (post-rename). Empty means nothing stamped; every mode engine stamps at
    plan compile (tests pin per-mode stamping).
    """

    table_name: str
    sql: str
    write_mode: Literal["create", "append", "replace"]
    view_name: str | None
    view_sql: str | None
    keys: TableKeys | None = None
    provenance: "Mapping[str, ColumnProvenance]" = field(default_factory=dict)
    kind_values: "Mapping[str, tuple[KindValueEntry, ...]]" = field(
        default_factory=dict
    )


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
                    provenance=spec.provenance,
                    kind_values=spec.kind_values,
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
                provenance=spec.provenance,
                kind_values=spec.kind_values,
            )
        )
    return ExportReport(tables=tuple(tables))
