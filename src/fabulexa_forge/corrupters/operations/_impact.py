"""Shared working-state helpers used by every operation handler.

Canonical-order population resolution paired with each row's physical index
(so a handler can mutate the exact working row it selected), pooled
multi-table population resolution and unit enumeration (canonical table
order for the three sampling handlers), the shared additive delta-draw
primitive, RowRef/locator construction from a working row, history-series
lookup (whether a records `prop__` has a tracked series in the working
`history` table), C7 group-shape recognition (`member__<f>__kind`/`__id`
pairs, `deactivated_at`), pinned-id membership, and the C6-mirror oracle
(anchor resolution, round-trip evaluation, series-unit enumeration) family C's
three handlers share. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § What each
operation breaks, § `RowRef`: the structural identity prefix,
`docs/architecture/pending/corrupter-grammar-v2.md` § The pooled population
and unit enumeration, and
`docs/architecture/pending/corrupter-history-sequence-operations.md` § The
impact rule -- mirroring C6 (normative).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_forge.corrupters.manifest import (
    CellLocator,
    ColumnLocator,
    RowLocator,
    RowRef,
)
from fabulexa_forge.corrupters.selection import (
    build_canonical_order_clause,
    build_predicate_clause,
    working_connection,
)
from fabulexa_forge.corrupters.state import WorkingTable
from fabulexa_forge.errors import CorruptError
from fabulexa_forge.reader.conformance import _ROUND_TRIPPABLE_TYPES, to_csv_text
from fabulexa_forge.reader.records_columns import ref_index_sibling

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping, Sequence

    from fabulexa_forge.config.models import Distribution
    from fabulexa_forge.corrupters.manifest import ImpactCode, RowCategory
    from fabulexa_forge.corrupters.state import CorruptState
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar, TableSpec

    _PooledUnit = tuple[int, int] | tuple[int, int, str] | tuple[int, int, str, str]
    """A pooled unit's shape: `(table_index, row_pos)`, optionally followed by
    `column` (cell units) and `target_kind` (`dangle_reference`'s and
    `mispoint_reference`'s eligible units) -- `unit_row_weights` only ever
    looks at the leading pair."""

_ROW_ID_COLUMN = "__rowid__"

_ROW_REF_KEYS: dict[str, tuple[str, ...]] = {
    "records": ("fork_path", "record_id"),
    "history": ("fork_path", "kind", "record_id", "property", "sim_time"),
    "membership": ("fork_path", "record_id", "joined_sim_time"),
}

_HISTORY_CONTRACT_COLUMNS: tuple[str, ...] = (
    *_ROW_REF_KEYS["history"],
    "value",
)
"""The six contract-pinned `history` columns (base-format.md § history)."""

SeriesKey = tuple[str, str, str]
"""A series' `(kind, property, record_id)` identity triple -- the parameter
order `resolve_c6_anchor` / `series_round_trip_fails` take, so a key unpacks
directly into either call. Shared by `drop_events` and `shift_sim_time`."""

PairKey = tuple[str, str]
"""A `(kind, property)` pair identity -- C11's converse grain, coarser than
`SeriesKey` (no `record_id`). Shared by `drop_events`' emptied-series clause."""


def is_round_trippable_type(type_string: str) -> bool:
    """Whether `type_string` names a C6 round-trippable column type.

    The one normalization every C6-mirroring gate shares — `.upper().strip()`
    against `_ROUND_TRIPPABLE_TYPES`, exactly the real `_check_c6`'s own
    comparison. A `schema_drift` retype stores the author's raw type literal
    verbatim on the working spec, so incidental whitespace must not change
    the verdict.

    Args:
        type_string: A DuckDB type literal, as carried on a ColumnSpec or an
            author's `retype_to` value.

    Returns:
        True iff the normalized type is one C6 round-trips.
    """
    return type_string.upper().strip() in _ROUND_TRIPPABLE_TYPES


def series_key(row: "Mapping[str, object]") -> SeriesKey:
    """A history row's series identity, for anchor/round-trip/timeline lookups."""
    kind = row["kind"]
    property_name = row["property"]
    record_id = row["record_id"]
    assert isinstance(kind, str)
    assert isinstance(property_name, str)
    assert isinstance(record_id, str)
    return (kind, property_name, record_id)


def draw_delta(distribution: "Distribution", rng: "random.Random") -> float:
    """One additive perturbation delta, drawn per `distribution`'s shape.

    Shared by `duplicate_rows`' near-duplicate jitter and `shift_sim_time`'s
    `offset` shift -- `Distribution`'s two consumers (design doc §
    Affected Subsystems).

    Args:
        distribution: The delta's shape (`uniform` or `normal`).
        rng: The operation's deterministic RNG sub-stream.

    Returns:
        One raw delta draw, in `distribution`'s units.
    """
    if distribution.shape == "uniform":
        assert distribution.low is not None and distribution.high is not None
        return rng.uniform(distribution.low, distribution.high)
    assert distribution.mean is not None and distribution.stddev is not None
    return rng.gauss(distribution.mean, distribution.stddev)


def row_category_for_table(spec: "TableSpec") -> "RowCategory":
    """The RowRef category `spec`'s rows use.

    Args:
        spec: The table's current (evolving) TableSpec.

    Returns:
        "records" / "membership" for those categories; "history" for a
        `fixed`-category table (the base contract's sole fixed-category table).
    """
    if spec.category == "records":
        return "records"
    if spec.category == "membership":
        return "membership"
    return "history"


def resolve_population_with_indices(
    working_table: "WorkingTable",
    fork_path: str,
    where: "Mapping[str, str] | None",
) -> tuple[pa.Table, list[int]]:
    """Resolve the current, filtered population in canonical content order,
    paired with each row's physical index into `working_table.data`.

    Registers the working table's *current* Arrow (tagged with a synthetic
    row-id column) as an ephemeral DuckDB relation and evaluates the
    fork_path + `where` predicate and the canonical ORDER BY there, exactly as
    `selection.resolve_population` does — but also returns the physical row
    index of each population row, so a handler can mutate the exact working
    row it selected rather than a byte-identical tie.

    Args:
        working_table: The table to select over, as currently mutated.
        fork_path: The sole branch's fork_path from the single-branch guard.
        where: The equality row filter, or None for all rows on fork_path.

    Returns:
        The filtered, canonically-ordered population content (matching
        `resolve_population`'s columns exactly), and the parallel list of each
        row's 0-based physical index into `working_table.data`.
    """
    n = working_table.data.num_rows
    tagged = working_table.data.append_column(
        _ROW_ID_COLUMN, pa.array(range(n), type=pa.int64())
    )
    predicate_clause = build_predicate_clause(working_table, fork_path, where)
    order_clause = build_canonical_order_clause(working_table)
    with working_connection(tagged) as conn:
        sql = f"SELECT * FROM working {predicate_clause} {order_clause}"
        result = conn.execute(sql).fetch_arrow_table()
    indices: list[int] = result.column(_ROW_ID_COLUMN).to_pylist()
    content = result.drop_columns([_ROW_ID_COLUMN])
    return content, indices


def series_timeline(
    history_table: "WorkingTable",
    fork_path: str,
    kind: str,
    record_id: str,
    property_name: str,
) -> tuple[pa.Table, list[int]]:
    """A series' full timeline -- ordered by sim_time ascending, ties by
    canonical content order -- and each row's physical index into
    `history_table.data`. Narrowed to `fork_path` and the series triple only,
    never by `target.where` (timeline locality is series-scoped, per § The
    impact rule). Shared by `freeze_series` and `shift_sim_time`.

    Args:
        history_table: The working `history` table, as of the operation's
            start.
        fork_path: The sole branch's fork_path.
        kind: The series' record kind.
        record_id: The series' record id.
        property_name: The series' property.

    Returns:
        The timeline content and each row's physical index into
        `history_table.data`, both in timeline order.
    """
    where = {"kind": kind, "record_id": record_id, "property": property_name}
    return resolve_population_with_indices(history_table, fork_path, where)


@dataclass(frozen=True)
class TablePopulation:
    """One resolved table's canonically-ordered, filtered population.

    Pairs `resolve_population_with_indices`' result with the table name and
    working table it came from, so pooled unit enumeration and per-table
    write-back can address the resolved set by index.
    """

    table_name: str
    """The resolved table's name, as `resolve_target_tables` returned it."""
    working_table: "WorkingTable"
    """The table's working state as of this operation's start."""
    content: pa.Table
    """The table's filtered, canonically-ordered population content."""
    physical_indices: list[int]
    """`content`'s rows' physical indices into `working_table.data`."""


def resolve_pooled_populations(
    state: "CorruptState",
    table_names: "Sequence[str]",
    fork_path: str,
    where: "Mapping[str, str] | None",
) -> list[TablePopulation]:
    """Resolve one filtered, canonically-ordered population per resolved table.

    Args:
        state: The engine's current working set.
        table_names: The operation's resolved table names, canonical
            (lexicographic) order.
        fork_path: The sole branch's fork_path from the single-branch guard.
        where: The operation's target.where, or None for all rows.

    A table missing one of `where`'s keys contributes zero units (§ The pooled
    population and unit enumeration): it is never queried (a `where` key
    absent from a table's schema would fail the equality predicate, not
    silently pass), so its population is the empty table of its own schema.

    Returns:
        One `TablePopulation` per `table_names` entry, in the same order.
    """
    populations: list[TablePopulation] = []
    for name in table_names:
        working_table = state.tables[name]
        column_names = {col.name for col in working_table.spec.columns}
        if where is not None and not set(where).issubset(column_names):
            content = working_table.data.schema.empty_table()
            physical_indices: list[int] = []
        else:
            content, physical_indices = resolve_population_with_indices(
                working_table, fork_path, where
            )
        populations.append(
            TablePopulation(name, working_table, content, physical_indices)
        )
    return populations


def enumerate_cell_units(
    populations: "Sequence[TablePopulation]",
    per_table_columns: "Sequence[Sequence[str]]",
) -> list[tuple[int, int, str]]:
    """Enumerate pooled cell units in canonical table -> row -> column order.

    Args:
        populations: The operation's resolved-table populations, canonical
            table order.
        per_table_columns: Each population's matched columns
            (`match_column_entries`' result), parallel to `populations`, in
            resolved-column order.

    Returns:
        `(table_index, row_pos, column)` triples, one per pooled cell unit,
        in canonical pooled order.
    """
    units: list[tuple[int, int, str]] = []
    for table_idx, (population, columns) in enumerate(
        zip(populations, per_table_columns)
    ):
        for row_pos in range(population.content.num_rows):
            for column in columns:
                units.append((table_idx, row_pos, column))
    return units


def write_back_pooled_columns(
    state: "CorruptState",
    populations: "Sequence[TablePopulation]",
    py_columns_by_table: "Sequence[dict[str, list[object]]]",
) -> None:
    """Write each population's mutated columns back into `state.tables`.

    Args:
        state: The engine's current working set, mutated in place.
        populations: The operation's resolved-table populations, canonical
            table order.
        py_columns_by_table: Each population's `{column: values}` overlay
            (physical-row-indexed), parallel to `populations`. A table with an
            empty overlay is left untouched.
    """
    for population, py_columns in zip(populations, py_columns_by_table):
        if not py_columns:
            continue
        new_data = population.working_table.data
        for column, values in py_columns.items():
            field_index = new_data.schema.get_field_index(column)
            pa_type = new_data.schema.field(field_index).type
            new_data = new_data.set_column(
                field_index, column, pa.array(values, type=pa_type)
            )
        state.tables[population.table_name] = WorkingTable(
            spec=population.working_table.spec, data=new_data
        )


def enumerate_row_units(
    populations: "Sequence[TablePopulation]", table_included: "Sequence[bool]"
) -> list[tuple[int, int]]:
    """Enumerate pooled row units in canonical table -> row order.

    Args:
        populations: The operation's resolved-table populations, canonical
            table order.
        table_included: Whether each population contributes row units,
            parallel to `populations` (`duplicate_rows` near mode excludes a
            table with zero matched jitter columns).

    Returns:
        `(table_index, row_pos)` pairs, one per pooled row unit, in canonical
        pooled order.
    """
    units: list[tuple[int, int]] = []
    for table_idx, (population, included) in enumerate(
        zip(populations, table_included)
    ):
        if not included:
            continue
        for row_pos in range(population.content.num_rows):
            units.append((table_idx, row_pos))
    return units


def placement_populations(
    populations: "Sequence[TablePopulation]",
) -> list[tuple[WorkingTable, pa.Table]]:
    """Adapt resolved-table populations to `derive_row_weights`' pair form.

    Args:
        populations: The operation's resolved-table populations, canonical
            table order.

    Returns:
        Each population's `(working_table, content)` pair, parallel order —
        `derive_row_weights`' `populations` parameter shape.
    """
    return [
        (population.working_table, population.content) for population in populations
    ]


def unit_row_weights(
    units: "Sequence[_PooledUnit]",
    row_weights: "Sequence[Sequence[float]]",
) -> list[float]:
    """Expand per-table, per-row placement weights to one weight per pooled unit.

    A cell unit inherits its row's weight (§ Placement: weights over units,
    normative): the weight looks only at each unit's leading
    `(table_index, row_pos)` pair, so cell units, row units, and
    `dangle_reference`'s `(table_index, row_pos, column, target_kind)`
    eligible units all share this one expansion.

    Args:
        units: The pooled units in canonical order; each entry's first two
            elements are `(table_index, row_pos)`.
        row_weights: `derive_row_weights`' result — one weight list per
            resolved table, each parallel to that table's population rows.

    Returns:
        One weight per unit, parallel to `units`.
    """
    return [row_weights[table_idx][row_pos] for table_idx, row_pos, *_ in units]


def current_value(
    py_columns: "Mapping[str, list[object]]",
    content: pa.Table,
    row_pos: int,
    physical_row: int,
    column: str,
) -> object:
    """A column's value as of this point in one operation's apply pass.

    Reads the live in-progress edit when `column` has already been touched by
    this operation (so a later cell in the same operation sees an earlier
    rewrite in the same row), else falls back to the pre-operation canonical-
    order snapshot. Shared by `null_cells` and `mutate_cells`.

    Args:
        py_columns: The operation's `{column: values}` overlay so far,
            physical-row-indexed.
        content: The table's filtered, canonically-ordered population content.
        row_pos: The cell's canonical-order row position in `content`.
        physical_row: The cell's physical row index into the working table.
        column: The cell's column.

    Returns:
        The column's current value at this cell.
    """
    if column in py_columns:
        return py_columns[column][physical_row]
    return content.column(column)[row_pos].as_py()


def row_dict(content: "pa.Table", row_pos: int) -> dict[str, object]:
    """One canonical-order row of `content`, as a `{column: value}` dict.

    Args:
        content: A population table (from `resolve_population_with_indices`).
        row_pos: The row's 0-based canonical-order position.

    Returns:
        A dict mapping every column of `content` to its value at `row_pos`.
    """
    return {
        name: content.column(name)[row_pos].as_py() for name in content.schema.names
    }


def build_row_ref(
    category: "RowCategory", spec: "TableSpec", row: "Mapping[str, object]"
) -> RowRef:
    """Build a `RowRef` for `row`'s structural identity prefix.

    Args:
        category: The row-identity scheme ("records" / "history" / "membership").
        spec: The table's current (evolving) TableSpec, for each key column's
            DuckDB type (the C6 text codec is type-directed).
        row: The row's `{column: value}` content, at minimum covering the
            category's identity-prefix columns.

    Returns:
        A RowRef carrying the category's fixed identity prefix, codec-rendered.
    """
    columns_by_name = {col.name: col for col in spec.columns}
    keys = tuple(
        (name, to_csv_text(row[name], columns_by_name[name].type))
        for name in _ROW_REF_KEYS[category]
    )
    return RowRef(category=category, keys=keys)


def cell_locator(
    table: str,
    category: "RowCategory",
    spec: "TableSpec",
    row: "Mapping[str, object]",
    column: str,
) -> CellLocator:
    """Build a `CellLocator` naming `column` on `row` of `table`."""
    return CellLocator(
        kind="cell", table=table, row=build_row_ref(category, spec, row), column=column
    )


def row_locator(
    table: str, category: "RowCategory", spec: "TableSpec", row: "Mapping[str, object]"
) -> RowLocator:
    """Build a `RowLocator` naming `row` of `table`."""
    return RowLocator(kind="row", table=table, row=build_row_ref(category, spec, row))


def column_locator(table: str, column: str) -> ColumnLocator:
    """Build a `ColumnLocator` naming `column` of `table` (no `RowRef`)."""
    return ColumnLocator(kind="column", table=table, column=column)


def is_membership_ref_column(name: str) -> bool:
    """Whether `name` is one half of a membership reference pair
    (`member__<f>__kind` / `member__<f>__id`)."""
    return name.startswith("member__") and (
        name.endswith("__kind") or name.endswith("__id")
    )


def is_membership_id_column(column: str) -> bool:
    """Whether `column` is a membership `member__<f>__id` reference column.

    Shared by `dangle_reference` and `mispoint_reference` -- both operations'
    population filters (1)+(2) and impact rules branch on this same test.
    """
    return column.startswith("member__") and column.endswith("__id")


def membership_partner_column(name: str) -> str:
    """The other half of `name`'s `member__<f>__kind` / `member__<f>__id` pair.

    Args:
        name: A `member__<f>__kind` or `member__<f>__id` column name.

    Returns:
        The partner column name.

    Raises:
        ValueError: `name` is neither a `__kind` nor a `__id` member column.
    """
    if name.endswith("__kind"):
        return name[: -len("__kind")] + "__id"
    if name.endswith("__id"):
        return name[: -len("__id")] + "__kind"
    raise ValueError(f"{name!r} is not a member__<f>__kind/__id column")


def records_reference_sibling(column: str, col_spec: "ColumnSpec") -> str | None:
    """The `ref_index__<name>` sibling column for a records reference
    `prop__` cell -- the pair-write target `null_cells`, `dangle_reference`,
    and `mispoint_reference` share whenever they rewrite a reference cell (an
    operation that rewrites a reference rewrites the edge, not a column).

    Args:
        column: The reference column's current name.
        col_spec: The column's current ColumnSpec.

    Returns:
        The paired `ref_index__<name>` column name when `column` is a
        records `prop__` reference (its `references` is set); None for a
        membership `member__<f>__id` reference or any non-reference column --
        neither carries a `ref_index__` analog.
    """
    if not column.startswith("prop__") or col_spec.references is None:
        return None
    return ref_index_sibling(column)


def membership_kind_id_pairs(state: "CorruptState") -> frozenset[tuple[str, str]]:
    """Every `(kind, id)` pair a non-NULL membership member pair carries,
    across every working membership-category table.

    Shared by `delete_rows` (the wake's C10 membership-reference test) and
    `insert_rows` (the id universe's membership-reference contribution) --
    both traverse every working `member__<f>__kind`/`__id` pair identically,
    differing only in what they do with the result (collect every pair vs.
    filter to one kind's ids).

    Args:
        state: The working set, as of the point this is called.

    Returns:
        The set of `(kind, id)` pairs any working membership row's
        `member__<f>__kind` / `member__<f>__id` pair carries; a pair with
        either half NULL is excluded, as is a `__kind` column whose partner
        `__id` column is absent from the table's current schema.
    """
    pairs: set[tuple[str, str]] = set()
    for working_table in state.tables.values():
        if working_table.spec.category != "membership":
            continue
        data = working_table.data
        column_names = set(data.schema.names)
        kind_columns = [
            name
            for name in column_names
            if name.startswith("member__") and name.endswith("__kind")
        ]
        for kind_col in kind_columns:
            id_col = membership_partner_column(kind_col)
            if id_col not in column_names:
                continue
            kinds = data.column(kind_col)
            ids = data.column(id_col)
            for i in range(data.num_rows):
                kind_val = kinds[i].as_py()
                id_val = ids[i].as_py()
                if kind_val is not None and id_val is not None:
                    pairs.add((kind_val, id_val))
    return frozenset(pairs)


def is_deactivated_at_column(name: str) -> bool:
    """Whether `name` is the C7-gated records `deactivated_at` column."""
    return name == "deactivated_at"


def property_name_for_prop_column(name: str) -> str:
    """The history `property` value backing a records `prop__<property>` column."""
    return name[len("prop__") :]


def history_series_exists(
    history_data: "pa.Table | None",
    fork_path: str,
    kind: str,
    record_id: str,
    property_name: str,
) -> bool:
    """Whether the working `history` table carries any row for this series.

    Existence only — not bounded by `slice_at` (a handler declares C6 even for
    a series with no history row at or before slice_at; that is a sound
    over-declaration, see § The impact rule).

    Args:
        history_data: The working `history` table's current Arrow, or None
            when the emit carries no `history` table.
        fork_path: The sole branch's fork_path.
        kind: The owning record's kind.
        record_id: The owning record's id.
        property_name: The tracked property name.

    Returns:
        True iff at least one working `history` row matches
        (fork_path, kind, record_id, property).
    """
    if history_data is None or history_data.num_rows == 0:
        return False
    fork_paths = history_data.column("fork_path")
    kinds = history_data.column("kind")
    record_ids = history_data.column("record_id")
    properties = history_data.column("property")
    for i in range(history_data.num_rows):
        if (
            fork_paths[i].as_py() == fork_path
            and kinds[i].as_py() == kind
            and record_ids[i].as_py() == record_id
            and properties[i].as_py() == property_name
        ):
            return True
    return False


def is_pinned_record_id(sidecar: "Sidecar", kind: str, record_id: str) -> bool:
    """Whether `record_id` is one of `kind`'s pinned ids in `sidecar`."""
    return record_id in sidecar.pinned_ids().get(kind, {}).values()


def _require_history_columns(table: pa.Table) -> None:
    """Raise unless `table` carries every contract-pinned `history` column.

    Args:
        table: A `history`-shaped Arrow table.

    Raises:
        CorruptError: `table` lacks one or more of the six contract-pinned
            history columns — an engine-invariant breach, not a config error.
    """
    present = set(table.schema.names)
    missing = [c for c in _HISTORY_CONTRACT_COLUMNS if c not in present]
    if missing:
        raise CorruptError(
            f"history table missing contract-pinned column(s): {', '.join(missing)}"
        )


def resolve_c6_anchor(
    history: pa.Table,
    fork_path: str,
    slice_at: int,
    kind: str,
    property_name: str,
    record_id: str,
) -> tuple[int, str] | None:
    """Resolve the (sim_time, value) pair C6 would select as a series' anchor.

    Mirrors `_check_c6`'s selection exactly: over the given working history
    rows narrowed to `fork_path` and the series triple, restricted to
    `sim_time <= slice_at`, rank by `(sim_time DESC, value DESC)` and return
    the rank-1 row's (sim_time, value) pair. Within a series the other four
    history columns are constant, so the pair is unique even when
    byte-identical duplicate rows tie completely -- which is what makes
    anchor participation (§ The impact rule) content-decidable.

    Args:
        history: A working history Arrow table (pre- or post-mutation state).
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at (sidecar-sourced).
        kind: The series' record kind.
        property_name: The series' property.
        record_id: The series' record id.

    Returns:
        The anchor (sim_time, value) pair, or None when the series' C6 view
        is empty (no row at or before slice_at).

    Raises:
        CorruptError: `history` lacks any of the six contract-pinned history
            columns -- an engine-invariant breach, not a config error.
    """
    _require_history_columns(history)
    fork_paths = history.column("fork_path")
    kinds = history.column("kind")
    record_ids = history.column("record_id")
    properties = history.column("property")
    sim_times = history.column("sim_time")
    values = history.column("value")

    best: tuple[int, str] | None = None
    for i in range(history.num_rows):
        if (
            fork_paths[i].as_py() != fork_path
            or kinds[i].as_py() != kind
            or record_ids[i].as_py() != record_id
            or properties[i].as_py() != property_name
        ):
            continue
        sim_time = sim_times[i].as_py()
        if sim_time > slice_at:
            continue
        value = values[i].as_py()
        candidate = (sim_time, value)
        if best is None or candidate > best:
            best = candidate
    return best


def series_round_trip_fails(
    state: "CorruptState",
    fork_path: str,
    slice_at: int,
    kind: str,
    property_name: str,
    record_id: str,
) -> bool:
    """Decide whether C6 would fail this series on the current working state.

    Mirrors `_check_c6` gate-for-gate against the working set: an empty C6
    view, an absent `records__<kind>` working table, an absent or
    non-round-trippable `prop__<property>` column (per the current
    `WorkingTable.spec`, honoring earlier `schema_drift`) cannot fail; a
    missing records row fails; otherwise EVERY records row matching
    (fork_path, record_id) is evaluated — the real check's LEFT JOIN fans the
    anchor out over duplicates (e.g. a `duplicate_rows` conflicting
    duplicate), so one NULL cell or one `to_csv_text(records cell)` mismatch
    (same codec, same `_ROUND_TRIPPABLE_TYPES` gate C6 uses) fails the
    series even when another matching row round-trips.

    Args:
        state: The shared working set, as of after the calling operation.
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at (sidecar-sourced).
        kind: The series' record kind.
        property_name: The series' property.
        record_id: The series' record id.

    Returns:
        True iff C6, run on an emit written from this state, would report
        this series as failing.

    Raises:
        CorruptError: The working history table is absent from the state --
            an engine-invariant breach, not a config error.
    """
    history_working = state.tables.get("history")
    if history_working is None:
        raise CorruptError(
            "series_round_trip_fails: working set carries no 'history' table"
        )
    anchor = resolve_c6_anchor(
        history_working.data, fork_path, slice_at, kind, property_name, record_id
    )
    if anchor is None:
        return False

    records_working = state.tables.get(f"records__{kind}")
    if records_working is None:
        return False

    prop_col = f"prop__{property_name}"
    columns_by_name = {col.name: col for col in records_working.spec.columns}
    col_spec = columns_by_name.get(prop_col)
    if col_spec is None:
        return False
    if not is_round_trippable_type(col_spec.type):
        return False

    records_data = records_working.data
    fork_paths = records_data.column("fork_path")
    record_ids = records_data.column("record_id")
    cell_column = records_data.column(prop_col)
    row_found = False
    for i in range(records_data.num_rows):
        if fork_paths[i].as_py() != fork_path or record_ids[i].as_py() != record_id:
            continue
        row_found = True
        cell_value: object = cell_column[i].as_py()
        if cell_value is None:
            return True
        if anchor[1] != to_csv_text(cell_value, col_spec.type):
            return True
    return not row_found


def branch_slice_at(sidecar: "Sidecar", fork_path: str) -> int:
    """The sole branch's `slice_at`, matched by `fork_path`.

    Args:
        sidecar: The source emit's sidecar.
        fork_path: The sole branch's fork_path (single-branch stage).

    Returns:
        That branch's sidecar-declared `slice_at`.
    """
    return next(b.slice_at for b in sidecar.branches() if b.fork_path == fork_path)


def anchor_participant_impact(
    row: "Mapping[str, object]",
    pre_anchor: "tuple[int, str] | None",
    round_trip_fails: bool,
) -> tuple["ImpactCode", ...]:
    """The shared anchor-participant impact rule for one removed history row.

    Used by both `drop_events` (dropped rows) and `freeze_series` (frozen /
    removed tail rows), per § The impact rule (normative). A removed row can
    only satisfy the anchor-participant rule's first disjunct: its (sim_time,
    value) pair equalled its series' anchor *before* this operation. `C6` iff
    that participation holds and the series' C6 round-trip fails on the
    post-operation state; `beyond-c1-c12` otherwise (a mid-series removal, an
    anchor removal whose codec text is unchanged, or a removal that empties
    the series' C6 view entirely).

    Args:
        row: The removed row's `{column: value}` content (at minimum
            `sim_time` and `value`).
        pre_anchor: The series' (sim_time, value) anchor pair *before* this
            operation, or None when the series' C6 view was already empty.
        round_trip_fails: Whether the series' C6 round-trip fails on the
            post-operation working state.

    Returns:
        `("C6",)` when the anchor participated and the round-trip now fails;
        `("beyond-c1-c12",)` otherwise.
    """
    is_anchor_participant = (
        pre_anchor is not None and (row["sim_time"], row["value"]) == pre_anchor
    )
    if is_anchor_participant and round_trip_fails:
        return ("C6",)
    return ("beyond-c1-c12",)


def with_c13(
    base: tuple["ImpactCode", ...], missing_genesis: bool
) -> tuple["ImpactCode", ...]:
    """Fold a C13 genesis break into a base impact tuple.

    C13 is a real conformance code, so it is mutually exclusive with the
    `beyond-c1-c12` sentinel (`_normalize_impact` rejects the mix): when the
    genesis break joins a base that is only the sentinel, C13 replaces it; when it
    joins real codes, C13 is added alongside. A no-op when `missing_genesis` is
    False. Shared by every operation whose C13 impact composes with another code
    (`schema_drift`'s C11, `shift_sim_time` / `drop_events`' C6, and
    `insert_rows`' sentinel-or-C13 base).

    Args:
        base: The operation's non-C13 impact tuple (real codes or the sentinel).
        missing_genesis: Whether the mutation leaves a record without its
            genesis history row (a C13 break).

    Returns:
        `base` unchanged when `missing_genesis` is False; otherwise `base` with
        the sentinel stripped and `"C13"` appended.
    """
    if not missing_genesis:
        return base
    codes: list[ImpactCode] = [c for c in base if c != "beyond-c1-c12"]
    codes.append("C13")
    return tuple(codes)


def kind_has_tracked_genesis_property(records_working: "WorkingTable") -> bool:
    """Whether `records_working`'s kind carries a C13 genesis obligation.

    True iff some `prop__` column is `history_tracked: true` with a
    round-trippable type -- exactly the flagged-column set `_check_c13_genesis`
    iterates. A record of such a kind that carries no `history` row (an
    `insert_rows` phantom) therefore lacks its genesis row for that property, so
    the phantom breaks C13.

    Args:
        records_working: A working records-category table.

    Returns:
        True iff the kind has at least one history_tracked, round-trippable
        `prop__` column.
    """
    return any(
        col.name.startswith("prop__")
        and col.history_tracked is True
        and is_round_trippable_type(col.type)
        for col in records_working.spec.columns
    )


def _record_created_sim_time(
    records_data: pa.Table, fork_path: str, record_id: str
) -> object | None:
    """`record_id`'s `created_sim_time` on `fork_path`, or None when absent.

    Returns None when the table lacks `record_id`/`created_sim_time` -- the same
    guard `_check_c13_genesis` uses to skip (not fail) a table missing the
    genesis-check columns.
    """
    names = set(records_data.schema.names)
    if "record_id" not in names or "created_sim_time" not in names:
        return None
    fork_paths = records_data.column("fork_path")
    record_ids = records_data.column("record_id")
    created = records_data.column("created_sim_time")
    for i in range(records_data.num_rows):
        if fork_paths[i].as_py() == fork_path and record_ids[i].as_py() == record_id:
            value: object = created[i].as_py()
            return value
    return None


def _history_has_genesis_row(
    history_data: "pa.Table | None",
    fork_path: str,
    kind: str,
    record_id: str,
    property_name: str,
    created_sim_time: object,
) -> bool:
    """Whether working `history` carries a row for this series at
    `created_sim_time` -- the genesis-row match `_check_c13_genesis` makes."""
    if history_data is None:
        return False
    fork_paths = history_data.column("fork_path")
    kinds = history_data.column("kind")
    record_ids = history_data.column("record_id")
    properties = history_data.column("property")
    sim_times = history_data.column("sim_time")
    for i in range(history_data.num_rows):
        if (
            fork_paths[i].as_py() == fork_path
            and kinds[i].as_py() == kind
            and record_ids[i].as_py() == record_id
            and properties[i].as_py() == property_name
            and sim_times[i].as_py() == created_sim_time
        ):
            return True
    return False


def series_missing_genesis_row(
    state: "CorruptState",
    fork_path: str,
    kind: str,
    property_name: str,
    record_id: str,
) -> bool:
    """Decide whether C13's genesis clause would fail this series on the working state.

    Mirrors `_check_c13_genesis` for a single (kind, property, record_id): the
    property must be `history_tracked: true` with a round-trippable type (per the
    current `WorkingTable.spec`, honoring earlier `schema_drift`), the record must
    exist (carry a `created_sim_time`), and working `history` must carry no row
    for the series at that `created_sim_time`. An untracked / non-round-trippable
    property, an absent `records__<kind>` table, or a missing records row cannot
    fail (returns False) -- C13's genesis clause does not apply there. Shared by
    `shift_sim_time` and `drop_events`, the operations that move or remove a
    genesis tick (parameter order matches `SeriesKey`, so a key unpacks directly).

    Args:
        state: The shared working set, as of after the calling operation.
        fork_path: The sole branch's fork_path.
        kind: The series' record kind.
        property_name: The series' property.
        record_id: The series' record id.

    Returns:
        True iff C13, run on an emit written from this state, would report this
        record as lacking its genesis history row for `property_name`.
    """
    records_working = state.tables.get(f"records__{kind}")
    if records_working is None:
        return False
    col_spec = next(
        (c for c in records_working.spec.columns if c.name == f"prop__{property_name}"),
        None,
    )
    if col_spec is None or col_spec.history_tracked is not True:
        return False
    if not is_round_trippable_type(col_spec.type):
        return False
    created = _record_created_sim_time(records_working.data, fork_path, record_id)
    if created is None:
        return False
    history_working = state.tables.get("history")
    history_data = history_working.data if history_working is not None else None
    return not _history_has_genesis_row(
        history_data, fork_path, kind, record_id, property_name, created
    )


def records_missing_genesis_for_property(
    history_data: "pa.Table | None",
    records_data: pa.Table,
    fork_path: str,
    kind: str,
    property_name: str,
) -> bool:
    """Whether any record of `kind` lacks its genesis history row for
    `property_name` -- C13's genesis clause over a whole (kind, property).

    Explicit-table form (records + history Arrow, no `state` spec lookup) for
    `schema_drift`, whose rename evolves the spec after this is computed: a
    tracked column renamed to a fresh `prop__<property_name>` has no matching
    history rows (history keeps the old property name), so every record loses its
    genesis -> C13. The caller applies the flagged-column gate (history_tracked +
    round-trippable) before calling.

    Args:
        history_data: The working `history` Arrow, or None.
        records_data: The working records Arrow for `kind` (carries `record_id`
            and `created_sim_time`).
        fork_path: The sole branch's fork_path.
        kind: The record kind.
        property_name: The property name (without its `prop__` prefix).

    Returns:
        True iff at least one `fork_path` record of `kind` has no history row for
        `property_name` at its own `created_sim_time`. False when the records
        table lacks `record_id`/`created_sim_time` -- the guard
        `_check_c13_genesis` uses to skip (not fail) the genesis check.
    """
    names = set(records_data.schema.names)
    if "record_id" not in names or "created_sim_time" not in names:
        return False
    fork_paths = records_data.column("fork_path")
    record_ids = records_data.column("record_id")
    created = records_data.column("created_sim_time")
    for i in range(records_data.num_rows):
        if fork_paths[i].as_py() != fork_path:
            continue
        if not _history_has_genesis_row(
            history_data,
            fork_path,
            kind,
            record_ids[i].as_py(),
            property_name,
            created[i].as_py(),
        ):
            return True
    return False


def history_pair_row_count(
    history_data: pa.Table, kind: str, property_name: str
) -> int:
    """The number of `history` rows for a `(kind, property)` pair.

    Args:
        history_data: A working `history` table's current Arrow.
        kind: The pair's record kind.
        property_name: The pair's property.

    Returns:
        The count of rows matching (kind, property), across every record_id and
        fork_path -- C11's converse grain (no series/fork narrowing).
    """
    kinds = history_data.column("kind")
    properties = history_data.column("property")
    return sum(
        1
        for i in range(history_data.num_rows)
        if kinds[i].as_py() == kind and properties[i].as_py() == property_name
    )


def c11_converse_broken(
    state: "CorruptState", history_data: pa.Table, kind: str, property_name: str
) -> bool:
    """Whether removing rows emptied a `(kind, property)` pair's C11 converse.

    True iff `history_data` (the post-removal working `history` Arrow) carries
    zero rows for `(kind, property)` while `records__<kind>` still has at least
    one row -- the emptied-series clause's grain (§ Corrupters — behavioral
    contracts, `drop_events` emptied-series clause). C11's converse is scoped to
    the whole `(kind, property)` pair, coarser than `SeriesKey`: emptying one
    record's series while a sibling keeps rows is not this clause.

    Args:
        state: The engine's current working set (records tables untouched by
            `drop_events`, so their row counts are pre- and post-removal alike).
        history_data: The post-removal working `history` Arrow.
        kind: The pair's record kind.
        property_name: The pair's property.

    Returns:
        True iff the emptied-series clause applies to this pair.
    """
    records_working = state.tables.get(f"records__{kind}")
    if records_working is None or records_working.data.num_rows == 0:
        return False
    return history_pair_row_count(history_data, kind, property_name) == 0


def enumerate_series_units(
    population: pa.Table,
    timeline_source: pa.Table,
    fork_path: str,
) -> tuple[tuple[str, str, str], ...]:
    """Enumerate `freeze_series`' pooled series units.

    A series (kind, record_id, property) qualifies when it appears among the
    narrowed population rows and its full timeline (all rows of
    `timeline_source` on `fork_path` with that triple) has at least two rows.
    Ordered lexicographically ascending by (kind, record_id, property) -- the
    canonical series order the unit draw, placement weights, and mode draws
    all index against.

    Args:
        population: The fork- and where-narrowed working history rows, in
            canonical content order.
        timeline_source: The full working history table; the helper narrows
            it to `fork_path` itself when resolving timelines.
        fork_path: The sole branch's fork_path.

    Returns:
        The ordered series-unit keys; empty when no series qualifies.

    Raises:
        CorruptError: Either table lacks any of the six contract-pinned
            history columns -- an engine-invariant breach, not a config error.
    """
    _require_history_columns(population)
    _require_history_columns(timeline_source)

    population_triples: set[tuple[str, str, str]] = set()
    for i in range(population.num_rows):
        kind = population.column("kind")[i].as_py()
        record_id = population.column("record_id")[i].as_py()
        property_name = population.column("property")[i].as_py()
        population_triples.add((kind, record_id, property_name))

    timeline_counts: dict[tuple[str, str, str], int] = {}
    src_fork_paths = timeline_source.column("fork_path")
    src_kinds = timeline_source.column("kind")
    src_record_ids = timeline_source.column("record_id")
    src_properties = timeline_source.column("property")
    for i in range(timeline_source.num_rows):
        if src_fork_paths[i].as_py() != fork_path:
            continue
        key = (
            src_kinds[i].as_py(),
            src_record_ids[i].as_py(),
            src_properties[i].as_py(),
        )
        timeline_counts[key] = timeline_counts.get(key, 0) + 1

    qualifying = sorted(
        triple for triple in population_triples if timeline_counts.get(triple, 0) >= 2
    )
    return tuple(qualifying)


def actor_subtype_undeclared(
    sidecar: "Sidecar", table_name: str, column: str, post_value: str
) -> bool:
    """The C12 predicate for one mutated records cell.

    Mirrors `_check_c12`'s actor sub-type coverage clause: True iff
    `table_name` is `records__actor`, `column` is `prop__actor_type`, the
    sidecar carries a subtyped `record_roles["actor"]`, and `post_value` is
    not one of its declared sub-types. False whenever `record_roles` is
    absent or `"actor"` is unregistered/non-subtyped there (C12 skips there --
    a declaration would be an empty-registry over-declaration).

    Args:
        sidecar: The source emit's sidecar.
        table_name: The mutated cell's table.
        column: The mutated cell's column.
        post_value: The cell's post-mutation stored value.

    Returns:
        True iff C12, run on an emit written from this state, would report
        this sub-type as undeclared.
    """
    if table_name != "records__actor" or column != "prop__actor_type":
        return False
    record_roles = sidecar.record_roles()
    if record_roles is None or "actor" not in record_roles.kinds():
        return False
    if not record_roles.is_subtyped("actor"):
        return False
    try:
        record_roles.role_of("actor", post_value)
    except KeyError:
        return True
    return False
