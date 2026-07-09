"""The versioned-intervals derivation: history → one row per (record_id, version).

Canonical relation: one row per (record_id, version) interval for a kind, over a
set of history-tracked properties. Version boundaries are the union of the tracked
properties' change points in history, set-deduplicated on (record_id, sim_time).
Each prop__<p> is the as-of last-known history.value at or before version_start
(codec VARCHAR). Ordered (record_id, version_start).

Layer-direction invariant: imports only the reader, fabulexa_export.errors,
and stdlib. Never imports exporters.* or config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from fabulexa_export.reader.sidecar import Sidecar

from fabulexa_export._sql import _sql_literal
from fabulexa_export.reader.relations import build_records_relation_sql

#: The fixed canonical columns; one prop__<p> column per tracked property follows,
#: in the kind's sidecar column-declaration order.
VERSIONED_INTERVAL_COLUMNS: tuple[str, ...] = (
    "record_id",
    "version_start",
    "version_end",
)


def _build_boundaries_cte(
    fork_path: str,
    kind: str,
    tracked_properties: frozenset[str],
    records_filter_sql: str | None,
) -> str:
    """Build the _boundaries CTE: deduplicated (record_id, sim_time) change points.

    UNIONs history rows for every tracked property under the given kind and
    fork_path, then SELECT DISTINCTs on (record_id, sim_time) to collapse
    same-sim_time boundaries from different properties into one. When a records
    filter is supplied, boundaries are restricted (semi-join) to the record_ids
    that filter selects — history carries no discriminator columns, so the
    restriction reads through the reader records relation.

    Args:
        fork_path: The sole branch fork_path (from require_single_branch).
        kind: The record kind whose history is reconstructed.
        tracked_properties: Non-empty set of history-tracked property names.
        records_filter_sql: A SELECT over records__<kind> whose record_ids bound
            the boundary set (discriminator-split sources), or None for all
            records of the kind.

    Returns:
        SQL for the _boundaries CTE body (without the WITH keyword).
    """
    selects: list[str] = []
    for prop in sorted(tracked_properties):
        selects.append(
            f'SELECT "record_id", "sim_time" FROM "history"'
            f' WHERE "fork_path" = {_sql_literal(fork_path)}'
            f' AND "kind" = {_sql_literal(kind)}'
            f' AND "property" = {_sql_literal(prop)}'
        )
    union_sql = " UNION ALL ".join(selects)
    boundaries = f'SELECT DISTINCT "record_id", "sim_time" FROM ({union_sql})'
    if records_filter_sql is not None:
        boundaries += (
            f' WHERE "record_id" IN (SELECT "record_id" FROM ({records_filter_sql}))'
        )
    return boundaries


def _build_versioned_cte(boundaries_alias: str) -> str:
    """Build the _versioned CTE: boundaries with LEAD(sim_time) as version_end.

    Args:
        boundaries_alias: The alias of the boundaries CTE to read from.

    Returns:
        SQL for the _versioned CTE body.
    """
    return (
        f'SELECT "record_id", "sim_time" AS "version_start",'
        f' LEAD("sim_time") OVER ('
        f'PARTITION BY "record_id" ORDER BY "sim_time"'
        f') AS "version_end"'
        f' FROM "{boundaries_alias}"'
    )


def _build_prop_asof_join(
    fork_path: str,
    kind: str,
    prop: str,
    versioned_alias: str,
) -> tuple[str, str]:
    """Build the ASOF LEFT JOIN that resolves one tracked property's as-of value.

    DuckDB's ASOF JOIN resolves the as-of lookback in a single sort-merge pass —
    for each version row it matches the one history row with the greatest
    sim_time at or before version_start. This mirrors
    row_state_events._build_prop_asof_join and replaces the former per-row
    correlated subquery, which the optimizer did not collapse and which scanned
    a record's whole history once per version row — O(versions x history) per
    record, quadratic when a single record concentrates many history rows (the
    exact shape that OOM'd the row-state-events fold at ~13 GB). LEFT keeps
    version rows with no prior history for the property (the prop reads NULL
    there); the single inequality (sim_time <= version_start) is inclusive,
    preserving the boundary-row-includes-its-own-change semantics the
    correlated subquery had.

    Args:
        fork_path: The sole branch fork_path.
        kind: The record kind.
        prop: The tracked property name (without the prop__ prefix).
        versioned_alias: The alias of the _versioned CTE.

    Returns:
        A 2-tuple (join_sql, value_expr): the ASOF LEFT JOIN clause (leading
        space, bound to a per-property alias) and the SELECT expression for the
        prop__<p> column.
    """
    alias = f"_h_{prop}"
    join_sql = (
        f' ASOF LEFT JOIN "history" AS "{alias}"'
        f' ON "{alias}"."fork_path" = {_sql_literal(fork_path)}'
        f' AND "{alias}"."kind" = {_sql_literal(kind)}'
        f' AND "{alias}"."property" = {_sql_literal(prop)}'
        f' AND "{alias}"."record_id" = "{versioned_alias}"."record_id"'
        f' AND "{alias}"."sim_time" <= "{versioned_alias}"."version_start"'
    )
    value_expr = f'"{alias}"."value" AS "prop__{prop}"'
    return join_sql, value_expr


def _ordered_tracked_properties(
    sidecar: "Sidecar",
    kind: str,
    tracked_properties: frozenset[str],
) -> list[str]:
    """Return tracked properties in sidecar column-declaration order.

    Queries the sidecar column list for records__<kind> to determine order.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind.
        tracked_properties: The set of tracked property names (without prop__ prefix).

    Returns:
        Property names in sidecar column-declaration order, restricted to
        tracked_properties.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    table_name = f"records__{kind}"
    cols = sidecar.columns(table_name)  # raises TableNotFoundError if absent
    ordered: list[str] = []
    for col in cols:
        if col.name.startswith("prop__"):
            prop = col.name[len("prop__") :]
            if prop in tracked_properties:
                ordered.append(prop)
    # Include any tracked properties not found in records (defensive; shouldn't happen)
    seen = set(ordered)
    for prop in sorted(tracked_properties):
        if prop not in seen:
            ordered.append(prop)
    return ordered


def build_versioned_intervals_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    tracked_properties: frozenset[str],
    discriminator_filter: Mapping[str, str],
) -> str:
    """Build the canonical versioned-intervals SELECT for a kind over history.

    One row per (record_id, version) interval. Version boundaries are the union of the
    tracked properties' change points in history, set-deduplicated on (record_id,
    sim_time) — two tracked properties changing at the same sim_time for a record yield
    one boundary, not two (matching today's SCD-2 SELECT DISTINCT kind, record_id,
    sim_time). Each interval's
    version_start / version_end are raw ns (version_end NULL on a record's last
    version, via LEAD). For each tracked property, the as-of value at version_start
    is projected as prop__<p>, codec VARCHAR — the most-recent history.value at or
    before version_start (one ASOF LEFT JOIN per property, resolved in a single
    sort-merge pass; NULL when no row precedes the boundary). When
    discriminator_filter is non-empty, the relation is restricted to the records
    matching the filter (semi-join on the reader records relation) — a
    discriminator-split source yields only the filtered sub-type's intervals.
    Reads history (filtered to the kind and the tracked properties),
    records__<kind> (only when a filter restricts the record set), and the
    sidecar (to order the prop columns). Static columns and per-source-type CAST are
    the mode's representation, not this relation: the mode composes the reader records
    relation (build_records_relation_sql) and LEFT JOINs it on record_id for them (see
    the versioned-intervals derivation design). Single-property → the history-interval
    grain; many → SCD-2 wide.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind whose history is reconstructed.
        tracked_properties: The history-tracked properties forming version
            boundaries; non-empty, pre-validated by the mode.
        discriminator_filter: Records column -> required value restricting the
            record set (a discriminator-split source's source.filter); an empty
            mapping selects the whole kind. Callers pass it explicitly — there is
            no default.

    Returns:
        A complete, deterministic SELECT producing VERSIONED_INTERVAL_COLUMNS
        followed by one prop__<p> per tracked property, ordered by
        (record_id, version_start).

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar (its column list
            orders the prop columns). The fold's primary read, history, raises
            nothing for absence: it is a contract-guaranteed fixed-category table
            (base-format C3), always present in a v4 emit.
    """
    ordered_props = _ordered_tracked_properties(sidecar, kind, tracked_properties)

    records_filter_sql: str | None = None
    if discriminator_filter:
        records_filter_sql = build_records_relation_sql(
            sidecar=sidecar,
            fork_path=fork_path,
            kind=kind,
            discriminator_filter=discriminator_filter,
        )

    boundaries_sql = _build_boundaries_cte(
        fork_path, kind, tracked_properties, records_filter_sql
    )
    versioned_sql = _build_versioned_cte("_boundaries")

    asof_joins: list[str] = []
    prop_exprs: list[str] = []
    for prop in ordered_props:
        join_sql, value_expr = _build_prop_asof_join(
            fork_path, kind, prop, "_versioned"
        )
        asof_joins.append(join_sql)
        prop_exprs.append(value_expr)

    select_cols = (
        '"_versioned"."record_id",'
        ' "_versioned"."version_start",'
        ' "_versioned"."version_end"'
    )
    if prop_exprs:
        select_cols += ", " + ", ".join(prop_exprs)

    asof_clause = "".join(asof_joins)

    return (
        f"WITH"
        f' "_boundaries" AS ({boundaries_sql}),'
        f' "_versioned" AS ({versioned_sql})'
        f" SELECT {select_cols}"
        f' FROM "_versioned"'
        f"{asof_clause}"
        f' ORDER BY "_versioned"."record_id", "_versioned"."version_start"'
    )
