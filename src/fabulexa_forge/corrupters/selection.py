"""The seeded selector surface shared by every corrupter operation.

The five-way table-selector resolution (`resolve_target_tables`), the
`target.columns` exact-or-pattern matcher (`match_column_entries`), canonical
content order, `target.where` evaluation (typed equality via DuckDB over the
registered *working* Arrow, reusing `render_typed_literal`'s coercion oracle),
and the seeded, without-replacement unit sampler `draw_sample`. See
`docs/architecture/pending/corrupter-grammar-v2.md` § Selector resolution,
§ Column entries: exact names and patterns, and
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Selection is
faithful; sampling is deterministic (normative).
"""

from __future__ import annotations

import fnmatch
import math
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from fabulexa_forge._sql import render_typed_literal
from fabulexa_forge.config.models import ClusteredTemporal, Correlated, EntityScoped
from fabulexa_forge.errors import CorruptValidationError

if TYPE_CHECKING:
    import random
    from collections.abc import Iterator, Mapping, Sequence

    import duckdb
    import pyarrow

    from fabulexa_forge.config.models import Amount, Target
    from fabulexa_forge.corrupters.state import WorkingTable
    from fabulexa_forge.reader.sidecar import Sidecar

_ROW_INDEX_COLUMN = "__placement_row_index__"


@contextmanager
def working_connection(table: "pyarrow.Table") -> "Iterator[duckdb.DuckDBPyConnection]":
    """Register `table` as the `working` relation on a fresh in-memory DuckDB
    connection, closing the connection on exit.

    The one connection-lifecycle idiom every selection-time DuckDB query
    shares: connect, register `table` as `working`, yield for the caller's
    query, close unconditionally.

    Args:
        table: The Arrow table to register as `working`.

    Yields:
        The connection, with `table` already registered as `working`.
    """
    import duckdb as duckdb_mod

    conn = duckdb_mod.connect(":memory:")
    try:
        conn.register("working", table)
        yield conn
    finally:
        conn.close()


def resolve_target_tables(target: "Target", sidecar: "Sidecar") -> list[str]:
    """Resolve a target's table selector to concrete table names, canonically
    ordered.

    Reads only sidecar table metadata (name, category, record_kind). The
    result is lexicographically ascending by table name — the canonical
    table order for pooling, unit enumeration, and defect emission.

    Args:
        target: The operation's target (exactly one selector field set,
            guaranteed at parse time).
        sidecar: The source emit's typed sidecar.

    Returns:
        The resolved table names, lexicographically ascending, non-empty.

    Raises:
        CorruptValidationError: The selector resolves to zero tables, or a
            `tables` entry names a table absent from the sidecar.
    """
    tables = sidecar.tables()
    if target.table is not None:
        if not any(t.name == target.table for t in tables):
            raise CorruptValidationError(f"table {target.table!r} is not in this emit")
        return [target.table]
    if target.tables is not None:
        known = {t.name for t in tables}
        for name in target.tables:
            if name not in known:
                raise CorruptValidationError(
                    f"tables entry {name!r} is not in this emit"
                )
        return sorted(target.tables)
    if target.glob is not None:
        matched = sorted(
            t.name for t in tables if fnmatch.fnmatchcase(t.name, target.glob)
        )
        if not matched:
            raise CorruptValidationError(
                f"glob {target.glob!r} matches no table in this emit"
            )
        return matched
    if target.category is not None:
        matched = sorted(t.name for t in tables if t.category == target.category)
        if not matched:
            raise CorruptValidationError(
                f"category {target.category!r} matches no table in this emit"
            )
        return matched
    assert target.record_kind is not None
    matched = sorted(t.name for t in tables if t.record_kind == target.record_kind)
    if not matched:
        raise CorruptValidationError(
            f"record_kind {target.record_kind!r} matches no table in this emit"
        )
    return matched


def match_column_entries(
    entries: "Sequence[str]", eligible_columns: "Sequence[str]"
) -> list[str]:
    """Resolve target.columns entries against one table's eligible columns.

    Each entry is an fnmatch pattern (entries without wildcard characters
    match exactly). Result order: entries in list order, each entry's
    matches in eligible-column order, deduplicated at first match. Pure and
    schema-agnostic — callers supply the operation's eligible-column list
    for the table's current (working or simulated) schema.

    Args:
        entries: The target.columns entries, exact or pattern.
        eligible_columns: The operation-eligible columns of one table, in
            working-schema column order.

    Returns:
        The matched column names for this table; possibly empty (a table
        where no entry matches contributes no units — the all-tables-empty
        case is rejected at validate time, not here).

    Raises:
        Nothing. Pure matching; the dead-entry case is `ColumnEntriesMatch`'s
        validate-time rule, not this function's.
    """
    matched: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for column in eligible_columns:
            if column in seen:
                continue
            if fnmatch.fnmatchcase(column, entry):
                matched.append(column)
                seen.add(column)
    return matched


def build_canonical_order_clause(working_table: "WorkingTable") -> str:
    """Build the ORDER BY clause imposing canonical content order.

    Orders by every column of the working table's *current* schema, ascending,
    NULLS FIRST, in schema column order — a pure function of row content (not
    physical position), so selection is deterministic regardless of DuckDB scan
    order and independent of Arrow input order.

    Args:
        working_table: The table whose current schema to order by.

    Returns:
        A SQL ORDER BY clause fragment, e.g. 'ORDER BY "a" ASC NULLS FIRST, ...'.
    """
    parts = [f'"{col.name}" ASC NULLS FIRST' for col in working_table.spec.columns]
    return "ORDER BY " + ", ".join(parts)


def build_predicate_clause(
    working_table: "WorkingTable",
    fork_path: str,
    where: Mapping[str, str] | None,
) -> str:
    """Build the WHERE clause narrowing a table to the sole fork_path and `where`.

    Every base table carries a `fork_path` column, so the fork_path equality is
    unconditional; each `target.where` `{column: value}` pair becomes
    `<column> = <render_typed_literal(value, column_type)>`, typed per the
    working table's *current* column type, conjoined with AND — reusing the one
    typed-literal coercion oracle the exporters already use, so DuckDB performs
    the cast and typed equality exactly as it does everywhere else.

    Args:
        working_table: The table to filter, as currently mutated.
        fork_path: The sole branch's fork_path from the single-branch guard.
        where: The equality row filter, or None for no additional filter.

    Returns:
        A SQL WHERE-clause fragment (including the WHERE keyword).
    """
    column_types = {col.name: col.type for col in working_table.spec.columns}
    predicates = [
        f'"fork_path" = {render_typed_literal(fork_path, column_types["fork_path"])}'
    ]
    if where:
        predicates.extend(
            f'"{column}" = {render_typed_literal(value, column_types[column])}'
            for column, value in where.items()
        )
    return "WHERE " + " AND ".join(predicates)


def resolve_population(
    working_table: "WorkingTable",
    fork_path: str,
    where: Mapping[str, str] | None,
) -> "pyarrow.Table":
    """Resolve the current, filtered population in canonical content order.

    Registers the working table's *current* Arrow as an ephemeral DuckDB
    relation and evaluates the fork_path + `where` predicate and the canonical
    ORDER BY there — so selection composes with mutations already applied to
    `working_table` (never re-reads the immutable source), and the row order is
    a pure function of content.

    Args:
        working_table: The table to select over, as currently mutated.
        fork_path: The sole branch's fork_path from the single-branch guard.
        where: The equality row filter, or None for all rows on fork_path.

    Returns:
        The filtered, canonically-ordered population as a pyarrow.Table.
    """
    predicate_clause = build_predicate_clause(working_table, fork_path, where)
    order_clause = build_canonical_order_clause(working_table)
    with working_connection(working_table.data) as conn:
        sql = f"SELECT * FROM working {predicate_clause} {order_clause}"
        return conn.execute(sql).fetch_arrow_table()


def _amount_k(amount: "Amount", n: int) -> int:
    """The seeded draw's exact size over a population of `n` units.

    Args:
        amount: The quantity rule (rate or count).
        n: The population size the rule applies over.

    Returns:
        floor(rate * n) for `rate`, min(count, n) for `count`.
    """
    if amount.rate is not None:
        return math.floor(amount.rate * n)
    assert amount.count is not None
    return min(amount.count, n)


def draw_sample(
    population_size: int, amount: "Amount", rng: "random.Random"
) -> list[int]:
    """Deterministically choose unit indices from a canonically-ordered population.

    Args:
        population_size: N, the number of units in canonical order.
        amount: The quantity rule (rate or count).
        rng: The operation's RNG sub-stream.

    Returns:
        The chosen 0-based indices, without replacement: floor(rate * N) for
        `rate`, min(count, N) for `count`; the empty list when N == 0.
    """
    if population_size == 0:
        return []
    k = _amount_k(amount, population_size)
    return rng.sample(range(population_size), k)


def draw_weighted_sample(
    weights: "Sequence[float]", amount: "Amount", rng: "random.Random"
) -> list[int]:
    """Deterministically choose unit indices by weighted sampling without
    replacement (Efraimidis-Spirakis).

    k is floor(rate * N) or min(count, N) over N = len(weights) — the full
    pooled population including zero-weight units. Draws one uniform
    (rng.random()) per unit in index order — every unit, zero-weight
    included, so stream consumption depends only on N — keys positive-weight
    units by u ** (1.0 / w), selects the min(k, positive-weight count)
    largest keys, ties broken by lower index. Zero-weight units are never
    chosen.

    Args:
        weights: Per-unit weights in pooled canonical unit order; >= 0.
        amount: The quantity rule (rate or count).
        rng: The operation's RNG sub-stream, positioned after placement
            setup draws.

    Returns:
        The chosen 0-based pooled unit indices, in ascending index order;
        empty when N == 0 or no unit has positive weight.
    """
    n = len(weights)
    if n == 0:
        return []
    k = _amount_k(amount, n)
    keys: list[tuple[float, int]] = []
    for index, weight in enumerate(weights):
        u = rng.random()
        if weight > 0:
            keys.append((u ** (1.0 / weight), index))
    if not keys:
        return []
    k = min(k, len(keys))
    keys.sort(key=lambda pair: (-pair[0], pair[1]))
    return sorted(index for _key, index in keys[:k])


def _project_indexed(content: "pyarrow.Table", projection: str) -> list[object]:
    """Project `projection` per row of `content`, aligned back to `content`'s
    row order via an explicit 0-based row index.

    A fresh DuckDB query over an already-ordered relation is not guaranteed to
    preserve that order absent an ORDER BY (`reader.md` § Determinism); tagging
    `content` with its row index and ordering by it is what recovers the
    alignment.

    Args:
        content: A population table (canonically ordered).
        projection: A SQL expression evaluated per row.

    Returns:
        `projection`'s value for each row of `content`, in `content`'s row
        order.
    """
    import pyarrow as pa

    n = content.num_rows
    tagged = content.append_column(
        _ROW_INDEX_COLUMN, pa.array(range(n), type=pa.int64())
    )
    with working_connection(tagged) as conn:
        sql = (
            f"SELECT {projection} AS __placement_value__ FROM working"
            f" ORDER BY {_ROW_INDEX_COLUMN}"
        )
        result = conn.execute(sql).fetch_arrow_table()
    values: list[object] = result.column("__placement_value__").to_pylist()
    return values


def _has_column(working_table: "WorkingTable", column: str) -> bool:
    """Whether `working_table`'s current schema declares `column`."""
    return any(col.name == column for col in working_table.spec.columns)


def _entity_scoped_weights(
    placement: "EntityScoped",
    populations: "Sequence[tuple[WorkingTable, pyarrow.Table]]",
    rng: "random.Random",
) -> list[list[float]]:
    """entity_scoped: weight 1 for rows whose record_id is in a seeded subset
    of the pooled population's distinct record_ids, 0 otherwise."""
    per_table_record_ids: list[list[str]] = [
        cast("list[str]", _project_indexed(content, '"record_id"'))
        for _working, content in populations
    ]
    universe = sorted({rid for ids in per_table_record_ids for rid in ids})
    k = _amount_k(placement.entities, len(universe))
    subset = set(rng.sample(universe, k))
    return [
        [1.0 if rid in subset else 0.0 for rid in ids] for ids in per_table_record_ids
    ]


def _clustered_temporal_weights(
    placement: "ClusteredTemporal",
    populations: "Sequence[tuple[WorkingTable, pyarrow.Table]]",
    rng: "random.Random",
) -> list[list[float]]:
    """clustered_temporal: weight 1 for rows whose column value falls within
    `width` of a seeded cluster center, 0 otherwise (including NULL values and
    tables lacking the column)."""
    per_table_values: list[list[int | None]] = []
    for working_table, content in populations:
        values: list[int | None]
        if _has_column(working_table, placement.column):
            values = cast(
                "list[int | None]",
                _project_indexed(content, f'"{placement.column}"'),
            )
        else:
            values = [None] * content.num_rows
        per_table_values.append(values)
    universe = sorted(
        {v for values in per_table_values for v in values if v is not None}
    )
    centers_count = min(placement.clusters, len(universe))
    centers = rng.sample(universe, centers_count)
    return [
        [
            1.0
            if v is not None and any(abs(v - c) <= placement.width for c in centers)
            else 0.0
            for v in values
        ]
        for values in per_table_values
    ]


def _correlated_weights(
    placement: "Correlated",
    populations: "Sequence[tuple[WorkingTable, pyarrow.Table]]",
) -> list[list[float]]:
    """correlated: `weight` where column equals value (typed equality), 1
    otherwise (non-match, NULL, or a table lacking the column). Consumes no
    RNG."""
    result: list[list[float]] = []
    for working_table, content in populations:
        column_spec = next(
            (col for col in working_table.spec.columns if col.name == placement.column),
            None,
        )
        if column_spec is None:
            result.append([1.0] * content.num_rows)
            continue
        literal = render_typed_literal(placement.value, column_spec.type)
        flags = _project_indexed(content, f'("{placement.column}" = {literal})')
        result.append([placement.weight if flag else 1.0 for flag in flags])
    return result


def derive_row_weights(
    placement: "EntityScoped | ClusteredTemporal | Correlated",
    populations: "Sequence[tuple[WorkingTable, pyarrow.Table]]",
    rng: "random.Random",
) -> list[list[float]]:
    """Derive per-row draw weights for the pooled population.

    Consumes the operation RNG stream first (before any unit draw): the
    entity_scoped subset or the clustered_temporal centers are drawn here via
    rng.sample over the sorted value universe; correlated draws nothing. Value
    comparisons use render_typed_literal's typed-equality oracle against each
    table's current column type, evaluated over the `where` ephemeral-relation
    mechanism with an explicit 0-based row index on the canonically-ordered
    population — flags come back ordered by that index, never by DuckDB
    result order. A table lacking the placement column takes the kind's
    absent-column weights (correlated -> 1, clustered_temporal -> 0).

    Args:
        placement: The operation's placement config.
        populations: Per resolved table, in canonical table order: the
            working table (for its current schema) and its filtered,
            canonically-ordered population.
        rng: The operation's RNG sub-stream.

    Returns:
        One weight list per table, parallel to `populations`, each parallel
        to that population's rows; weights >= 0.
    """
    if isinstance(placement, EntityScoped):
        return _entity_scoped_weights(placement, populations, rng)
    if isinstance(placement, ClusteredTemporal):
        return _clustered_temporal_weights(placement, populations, rng)
    return _correlated_weights(placement, populations)
