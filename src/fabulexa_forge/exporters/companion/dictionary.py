"""Shared data-dictionary resolution for the README and manifest companions.

Both companions render the same resolved dictionary -- README prose and the
manifest's machine-readable mirror must never disagree -- so the resolution
lives here once and each companion builder renders it its own way (design
doc § The data dictionary in companion artifacts).

**Column resolution.** A column with no carried provenance entry (computed,
or fed by more than one source) inherits nothing. A carried column's
documentation is its source column's resolved `ColumnDoc`. Unit inheritance
stops where a rendering election left the source's raw-nanosecond ("ns")
form for something else -- a temporal/instant rendering turns a raw ns
integer into a DATE/TIMESTAMPTZ value the unit no longer describes; every
other declared unit rides its rendering (decimal, json_precision,
cast-back) unchanged, since none of those change *what* the value counts.

**Table resolution.** A table's description forwards iff every one of its
carried columns agrees on a single source table -- the "one authority"
resolution generalised to table granularity. A table spanning several
source tables (a dimensional lookup, the source event log's multi-kind
union) forwards nothing; there is no single subject to attribute it to.

**Closed-domain columns.** A carried column whose source is a
`records__<kind>` table's `prop__<name>` (or bare-named) property renders
its declared value list when the sidecar carries an `enum_domains` entry
for `(kind, property)`; every other carried column has none.

**Kind-name-as-value columns.** Resolved straight from `TableReport.kind_values`
-- each rendered label glossed by its source kind's table description.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.exporters.query_spec import TableReport
    from fabulexa_forge.reader.documentation import ColumnDoc, Documentation, EnumOption

_RECORDS_TABLE_PREFIX = "records__"
_PROP_COLUMN_PREFIX = "prop__"

#: history_interval's virtual next-sim_time column (dimensional/validation.py's
#: `_LEAD_SIM_TIME`) -- not a declared sidecar column, so it carries no sidecar
#: entry of its own. It is `LEAD(sim_time)` (grains.py's history-interval
#: builder): the value it holds literally *is* the next row's `sim_time`, so
#: its documentation is `sim_time`'s.
_LEAD_SIM_TIME_COLUMN = "lead_sim_time"
_HISTORY_TABLE = "history"

#: DuckDB's integer type literals -- the forms a raw-nanosecond ("ns") value
#: still counts as itself under. Any other rendered type is a
#: temporal/instant rendering that has left the "ns" claim behind.
_INTEGER_DUCKDB_TYPES = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)


def records_kind_from_table(table_name: str) -> str | None:
    """The kind name for a `records__<kind>` sidecar table, else None.

    Args:
        table_name: A sidecar table name, as carried on a `ColumnProvenance`.

    Returns:
        The kind name, or None for a table outside the `records__` family
        (membership, fixed).
    """
    if not table_name.startswith(_RECORDS_TABLE_PREFIX):
        return None
    return table_name[len(_RECORDS_TABLE_PREFIX) :]


def resolve_table_description(doc: "Documentation", table: "TableReport") -> str | None:
    """One table's forwarded description.

    Args:
        doc: The emit's documentation view.
        table: The output table report.

    Returns:
        The single source table's `tables[].description`, when every
        carried column agrees on one source table; None when the table
        carries no provenance, or its carried columns span more than one
        source table.
    """
    sources = {entry.source_table for entry in table.provenance.values()}
    if len(sources) != 1:
        return None
    return doc.table_description(next(iter(sources)))


def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """One output column's resolved documentation.

    Args:
        doc: The emit's documentation view.
        table: The output table report.
        column_name: The output column name (post-rename).
        output_type: The column's materialized DuckDB type text.

    Returns:
        The source column's resolved `ColumnDoc` (history_interval's virtual
        `lead_sim_time` resolves through `sim_time`'s, since it *is* the next
        row's `sim_time`), unit dropped where `output_type` shows the
        rendering left the source's raw-nanosecond form behind; None for a
        column with no carried provenance, or whose source carries neither
        description nor unit.
    """
    provenance = table.provenance.get(column_name)
    if provenance is None:
        return None
    source_column = provenance.source_column
    if (
        provenance.source_table == _HISTORY_TABLE
        and source_column == _LEAD_SIM_TIME_COLUMN
    ):
        source_column = "sim_time"
    column_doc = doc.column_doc(provenance.source_table, source_column)
    if column_doc is None:
        return None
    if column_doc.unit == "ns" and output_type.upper() not in _INTEGER_DUCKDB_TYPES:
        return replace(column_doc, unit=None)
    return column_doc


def resolve_column_enum_options(
    doc: "Documentation", table: "TableReport", column_name: str
) -> "tuple[EnumOption, ...]":
    """One output column's declared value list.

    Args:
        doc: The emit's documentation view.
        table: The output table report.
        column_name: The output column name (post-rename).

    Returns:
        The source property's declared options, in sidecar order; empty
        when the column carries no provenance, its source table is not a
        `records__<kind>` table, or the source `(kind, property)` declares
        no closed domain.
    """
    provenance = table.provenance.get(column_name)
    if provenance is None:
        return ()
    kind = records_kind_from_table(provenance.source_table)
    if kind is None:
        return ()
    prop = provenance.source_column.removeprefix(_PROP_COLUMN_PREFIX)
    try:
        return doc.enum_options(kind, prop)
    except KeyError:
        return ()


def resolve_kind_value_glosses(
    doc: "Documentation", table: "TableReport", column_name: str
) -> tuple[tuple[str, str | None], ...]:
    """One kind-name-as-value column's per-label gloss list.

    Args:
        doc: The emit's documentation view.
        table: The output table report.
        column_name: The output column name (post-rename).

    Returns:
        `(label, gloss)` pairs in the column's `kind_values` order -- the
        gloss is the label's source kind's table description, or None when
        that kind's table carries no description. Empty when the column has
        no `kind_values` entry.
    """
    entries = table.kind_values.get(column_name, ())
    return tuple(
        (
            entry.label,
            doc.table_description(f"{_RECORDS_TABLE_PREFIX}{entry.source_kind}"),
        )
        for entry in entries
    )
