"""Shared data-dictionary resolution for the README and manifest companions.

Both companions render the same resolved dictionary -- README prose and the
manifest's machine-readable mirror must never disagree -- so the resolution
lives here once and each companion builder renders it its own way (design
doc § The data dictionary in companion artifacts).

**Column resolution.** The export config's `author_descriptions` (dimensional
`columns[].description`, source `descriptions`, base `rename[].descriptions`
— stamped onto `TableReport.author_descriptions` at plan compile, keyed by
output column name) is consulted first: an entry re-voices a carried
column's description while its unit still inherits under the rules below, or
gives a column with no carried provenance a description-only doc where one
otherwise wouldn't exist. Without an entry, resolution is exactly the
carried-column rule that follows.

A column with no carried provenance entry (computed,
or fed by more than one source) inherits nothing. A carried column's
documentation is its source column's resolved `ColumnDoc` — except the
pinned structural strings whose prose points at base-layer structure a
shaped export does not contain, which render here with that pointer clause
rewritten out (`_EXPORT_STRUCTURAL_REWRITES`; the contract's § Structural
column descriptions makes verbatim embedding a MAY, and a renamed export
has left the naming domain those clauses point into). Unit inheritance
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
for `(kind, property)`; every other carried column has none. A
value-mapped carry declares the *post-map* domain — the source options
translated through the stamped `ColumnProvenance.value_map`, glosses kept,
unmapped options dropped (they render NULL) — never the source property's
raw values, which the column does not contain.

**Kind-name-as-value columns.** Resolved straight from `TableReport.kind_values`
-- each rendered label glossed by its source kind's table description.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from fabulexa_forge.reader.documentation import ColumnDoc, EnumOption
from fabulexa_forge.reader.records_columns import (
    RECORDS_TABLE_PREFIX,
    records_kind_from_table,
)

if TYPE_CHECKING:
    from fabulexa_forge.exporters.query_spec import ColumnProvenance, TableReport
    from fabulexa_forge.reader.documentation import Documentation

_PROP_COLUMN_PREFIX = "prop__"

#: history_interval's virtual next-sim_time column (dimensional/validation.py's
#: `_LEAD_SIM_TIME`) -- not a declared sidecar column, so it carries no sidecar
#: entry of its own. It is `LEAD(sim_time)` (grains.py's history-interval
#: builder): the *value* is the next row's `sim_time`, but its *meaning* on
#: the interval row is the end of the value's validity — so it resolves
#: through `sim_time`'s entry for unit/origin and carries the forge-authored
#: end-of-interval description below, never `sim_time`'s took-effect prose.
_LEAD_SIM_TIME_COLUMN = "lead_sim_time"
_HISTORY_TABLE = "history"

#: The interval-end description authored here: the contract documents only
#: the one `sim_time` axis; the [start, end) reading is a consumer
#: derivation, so its end column's prose is forge's to own.
_LEAD_SIM_TIME_DESCRIPTION = (
    "Simulation time the value stopped holding — the instant the series'"
    " next change took effect; NULL while the value is still current at the"
    " slice boundary."
)

#: The `membership__<K>__<p>` table-name prefix — family membership only;
#: nothing here parses the owner kind or property out of the name.
_MEMBERSHIP_TABLE_PREFIX = "membership__"

#: The source event log's forge-pinned documentation (design doc § The
#: pinned event-log documentation) — mode-definitional, like the log's fixed
#: column set and first id; no config surface exists. Applied only to a
#: `TableReport` marked `event_log=True`.
_EVENT_LOG_TABLE_DESCRIPTION = (
    "The change log: one row per change to an audited item — a creation, an"
    " update, or a deletion — in event order."
)

_EVENT_LOG_COLUMN_DESCRIPTIONS: "dict[str, str]" = {
    "id": (
        "Sequence number of this log row: dense, ascending in event order,"
        " starting at 1."
    ),
    "item_type": (
        "The type of the changed item. The values listed below name each"
        " audited item type."
    ),
    "item_id": (
        "Identifier of the changed item, scoped by item_type: one item"
        " keeps one identifier across its rows."
    ),
    "event": "What happened to the item: 'create', 'update', or 'destroy'.",
    "occurred_at": (
        "When the change took effect. Changes are logged in order, so this"
        " never decreases as id ascends."
    ),
    "changes": (
        "JSON object of the fields this change touched, each mapped to an"
        " [old, new] value pair — old is null on a creation, new is null on"
        " a deletion."
    ),
}

#: Export-facing rewrites of the pinned structural strings whose prose
#: points at base-layer structure a shaped export does not contain — a
#: `records__<kind>` table to equality-join, the `membership__<K>__<p>`
#: table-name shape ("the table name's <K> segment"), the `record_index`
#: column most shaped exports render under another name or not at all, and
#: the sidecar ("present only when the sidecar declares it" beside a column
#: that visibly is present). The contract *permits* verbatim embedding
#: ("MAY embed the strings below verbatim", contract § Structural column
#: descriptions) — permission, not obligation; an export has left the base
#: layer's naming domain, so each rewrite keeps the string's factual core
#: and drops only the dangling pointer clause. Keyed by (pinned family,
#: source column); applied only to a contract-answered doc, never to
#: sidecar prose; units are untouched.
_EXPORT_STRUCTURAL_REWRITES: "dict[tuple[str, str], str]" = {
    ("history", "record_id"): "Id of the record whose property changed. Opaque.",
    ("records", "record_id"): (
        "Opaque identifier of the record within its branch and kind. Not"
        " ordered by creation."
    ),
    ("records", "presentation_id"): (
        "Presentation surrogate identity minted for this kind."
    ),
    ("membership", "record_id"): "Id of the record that owns the collection.",
}


def _structural_family(source_table: str) -> str | None:
    """The pinned-block family a provenance source table answers under.

    Args:
        source_table: A `ColumnProvenance.source_table` value.

    Returns:
        "history" / "records" / "membership", or None for a source table
        outside the three pinned families.
    """
    if source_table == _HISTORY_TABLE:
        return "history"
    if records_kind_from_table(source_table) is not None:
        return "records"
    if source_table.startswith(_MEMBERSHIP_TABLE_PREFIX):
        return "membership"
    return None


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


def resolve_table_description(doc: "Documentation", table: "TableReport") -> str | None:
    """One table's resolved description, author-first.

    Args:
        doc: The emit's documentation view.
        table: The output table report.

    Returns:
        The report's author table description when present; else the pinned
        event-log table description when the report is marked as the event
        log; else the single source table's `tables[].description` when
        every carried column agrees on one source table; else None.
    """
    if table.author_table_description is not None:
        return table.author_table_description
    if table.event_log:
        return _EVENT_LOG_TABLE_DESCRIPTION
    sources = {entry.source_table for entry in table.provenance.values()}
    if len(sources) != 1:
        return None
    return doc.table_description(next(iter(sources)))


def _resolve_source_doc(
    doc: "Documentation", provenance: "ColumnProvenance"
) -> "ColumnDoc | None":
    """A carried column's source-resolved doc, pre-rewrite, pre-unit-stop.

    Args:
        doc: The emit's documentation view.
        provenance: The carried column's provenance entry.

    Returns:
        history_interval's virtual `lead_sim_time` resolves through
        `sim_time`'s entry for unit/origin but carries the forge-authored
        end-of-interval description — the start column's took-effect prose
        would be wrong on the end bound; every other carried column resolves
        straight through `Documentation.column_doc`. None when the source
        carries neither description nor unit.
    """
    if (
        provenance.source_table == _HISTORY_TABLE
        and provenance.source_column == _LEAD_SIM_TIME_COLUMN
    ):
        sim_time_doc = doc.column_doc(_HISTORY_TABLE, "sim_time")
        return (
            None
            if sim_time_doc is None
            else replace(sim_time_doc, description=_LEAD_SIM_TIME_DESCRIPTION)
        )
    return doc.column_doc(provenance.source_table, provenance.source_column)


def _ns_unit_survives(unit: str | None, output_type: str) -> bool:
    """Whether a carried "ns" unit still describes `output_type`'s rendering.

    A rendering election that turned a raw-nanosecond integer into a
    DATE/TIMESTAMPTZ value has left the "ns" claim behind; every other
    declared unit rides its rendering unchanged.

    Args:
        unit: The source doc's resolved unit, or None.
        output_type: The column's materialized DuckDB type text.

    Returns:
        False only when `unit == "ns"` and `output_type` is not one of
        DuckDB's integer type literals; True otherwise (including unit=None).
    """
    return not (unit == "ns" and output_type.upper() not in _INTEGER_DUCKDB_TYPES)


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
        On a report marked as the event log, a column named in the pinned
        event-log set resolves to a description-only `ColumnDoc` with origin
        "forge" (author entries cannot exist there; nothing inherits there
        today). Otherwise, with an `author_descriptions` entry for the
        column: the resolved doc with the author's description and origin
        "author" — on a carried column the inherited unit rides along under
        today's unit rules; on a column with no carried provenance the doc
        is description-only (unit None). Without an entry: exactly today's
        resolution — the source column's resolved `ColumnDoc`
        (history_interval's virtual `lead_sim_time` case above), a
        contract-answered description rewritten per
        `_EXPORT_STRUCTURAL_REWRITES` where the pinned string points at
        base-layer structure the export does not contain, unit dropped
        where `output_type` shows the rendering left the source's
        raw-nanosecond form behind; None for a column with no carried
        provenance, or whose source carries neither description nor unit.
    """
    if table.event_log:
        pinned = _EVENT_LOG_COLUMN_DESCRIPTIONS.get(column_name)
        if pinned is not None:
            return ColumnDoc(description=pinned, unit=None, origin="forge")
    provenance = table.provenance.get(column_name)
    override = table.author_descriptions.get(column_name)
    if override is not None:
        source_doc = (
            None if provenance is None else _resolve_source_doc(doc, provenance)
        )
        unit = (
            source_doc.unit
            if source_doc is not None
            and _ns_unit_survives(source_doc.unit, output_type)
            else None
        )
        return ColumnDoc(description=override, unit=unit, origin="author")
    if provenance is None:
        return None
    column_doc = _resolve_source_doc(doc, provenance)
    if column_doc is None:
        return None
    if column_doc.origin == "contract":
        family = _structural_family(provenance.source_table)
        rewrite = (
            None
            if family is None
            else _EXPORT_STRUCTURAL_REWRITES.get((family, provenance.source_column))
        )
        if rewrite is not None:
            column_doc = replace(column_doc, description=rewrite)
    if not _ns_unit_survives(column_doc.unit, output_type):
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
        no closed domain. A value-mapped carry (`provenance.value_map` set)
        declares the values the column actually renders: each source option
        is translated through the map, keeping its gloss; a source option
        the map omits is dropped — it renders NULL, outside the declared
        domain.
    """
    provenance = table.provenance.get(column_name)
    if provenance is None:
        return ()
    kind = records_kind_from_table(provenance.source_table)
    if kind is None:
        return ()
    prop = provenance.source_column.removeprefix(_PROP_COLUMN_PREFIX)
    try:
        options = doc.enum_options(kind, prop)
    except KeyError:
        return ()
    if provenance.value_map is None:
        return options
    mapped = dict(provenance.value_map)
    return tuple(
        EnumOption(value=mapped[option.value], description=option.description)
        for option in options
        if option.value in mapped
    )


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
            doc.table_description(f"{RECORDS_TABLE_PREFIX}{entry.source_kind}"),
        )
        for entry in entries
    )
