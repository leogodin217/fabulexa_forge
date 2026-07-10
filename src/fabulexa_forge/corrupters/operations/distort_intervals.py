"""`distort_intervals`: rewrite sampled membership intervals' timing boundaries.

The two pure enumeration functions this operation builds on: member-timeline
identity and within-timeline adjacency (`enumerate_member_timelines`), and the
three modes' unit populations (`enumerate_interval_units`); and
`DistortIntervalsCorrupter`, the handler that pools those units across the
resolved membership tables, draws a sample, and rewrites the drawn units'
timing cells. See `docs/architecture/corrupters.md` § Member timelines and
adjacency, § distort_intervals: modes, populations, and rewrites, § What each
operation breaks, and the impact it declares, § Placement: weights over units
(normative).
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, NamedTuple

import pyarrow as pa

from fabulexa_forge.config.models import DistortIntervals
from fabulexa_forge.corrupters.manifest import DefectRecord
from fabulexa_forge.corrupters.operations._impact import (
    TablePopulation,
    branch_slice_at,
    cell_locator,
    resolve_pooled_populations,
    row_dict,
    row_locator,
    unit_row_weights,
    write_back_pooled_columns,
)
from fabulexa_forge.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    resolve_target_tables,
)
from fabulexa_forge.corrupters.state import OperationOutcome
from fabulexa_forge.errors import CorruptError

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping, Sequence

    from fabulexa_forge.config.models import CorruptOperation
    from fabulexa_forge.corrupters.manifest import CellLocator, RowLocator
    from fabulexa_forge.corrupters.state import CorruptState, WorkingTable
    from fabulexa_forge.reader.sidecar import Sidecar, TableSpec

_STRUCTURAL_COLUMNS: tuple[str, ...] = (
    "fork_path",
    "record_id",
    "joined_sim_time",
    "left_sim_time",
)
"""Every membership table's fixed identity/interval-boundary columns --
excluded from a member timeline's element-field identity, per § Member
timelines and adjacency."""

_MODES: tuple[str, ...] = ("overlap", "gap", "left_before_join")


def _require_membership_columns(working: pa.Table) -> None:
    """Raise unless `working` carries every structural membership column.

    Args:
        working: A membership-shaped Arrow table.

    Raises:
        CorruptError: `working` lacks fork_path, record_id, joined_sim_time,
            or left_sim_time -- an engine-invariant breach, not a config error.
    """
    present = set(working.schema.names)
    missing = [name for name in _STRUCTURAL_COLUMNS if name not in present]
    if missing:
        raise CorruptError(
            "membership table missing structural column(s): " + ", ".join(missing)
        )


def _canonical_sort_key(value: object) -> tuple[int, object]:
    """A NULLS-FIRST ascending key for one column value -- mirrors
    `build_canonical_order_clause`'s "ASC NULLS FIRST" ordering, computed in
    Python rather than via a DuckDB round trip."""
    return (0, None) if value is None else (1, value)


def _row_canonical_key(
    row: "Mapping[str, object]", schema_names: "Sequence[str]"
) -> tuple[tuple[int, object], ...]:
    """The whole-row canonical content order key: every column of the
    table's schema, ascending, NULLS FIRST, in schema column order."""
    return tuple(_canonical_sort_key(row[name]) for name in schema_names)


def _row_sort_key(
    rows: "Sequence[Mapping[str, object]]",
    schema_names: "Sequence[str]",
    index: int,
) -> tuple[object, tuple[tuple[int, object], ...], int]:
    """Within-timeline row order key: `joined_sim_time` ascending, ties by
    canonical content order, byte-identical ties broken by physical index
    (a deterministic, stable-across-calls tie-break)."""
    row = rows[index]
    return (row["joined_sim_time"], _row_canonical_key(row, schema_names), index)


def _timeline_sort_key(
    rows: "Sequence[Mapping[str, object]]",
    schema_names: "Sequence[str]",
    timeline: tuple[int, ...],
) -> tuple[tuple[tuple[int, object], ...], int]:
    """Timeline order key: the first (already within-timeline-ordered) row's
    canonical content order."""
    first_index = timeline[0]
    return (_row_canonical_key(rows[first_index], schema_names), first_index)


def enumerate_member_timelines(
    working: pa.Table,
    fork_path: str,
) -> tuple[tuple[int, ...], ...]:
    """Group one working membership table's rows into member timelines.

    A timeline is the rows sharing (record_id, every element-field value) on
    `fork_path` -- element-field columns are all columns other than fork_path,
    record_id, joined_sim_time, left_sim_time, compared by typed equality on
    working values, NULL grouping with NULL. Within a timeline, row indices
    order by joined_sim_time ascending, ties by canonical content order
    (byte-identical ties interchangeable). Timelines order by their first
    row's canonical content order -- the canonical unit order the draw,
    placement weights, and rewrites all index against.

    Args:
        working: The full working membership table (never where-narrowed --
            adjacency is a whole-timeline property).
        fork_path: The sole branch's fork_path.

    Returns:
        Ordered timelines, each an ordered tuple of row indices into
        `working`; empty when the table has no rows on `fork_path`.

    Raises:
        CorruptError: `working` lacks fork_path, record_id, joined_sim_time,
            or left_sim_time -- an engine-invariant breach, not a config error.
    """
    _require_membership_columns(working)
    schema_names = working.schema.names
    element_fields = [name for name in schema_names if name not in _STRUCTURAL_COLUMNS]
    rows: list[dict[str, object]] = working.to_pylist()

    groups: dict[tuple[object, tuple[object, ...]], list[int]] = {}
    group_order: list[tuple[object, tuple[object, ...]]] = []
    for index, row in enumerate(rows):
        if row["fork_path"] != fork_path:
            continue
        key = (row["record_id"], tuple(row[name] for name in element_fields))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(index)

    timelines = [
        tuple(
            sorted(
                groups[key], key=functools.partial(_row_sort_key, rows, schema_names)
            )
        )
        for key in group_order
    ]
    timelines.sort(key=functools.partial(_timeline_sort_key, rows, schema_names))
    return tuple(timelines)


def _overlap_pair_qualifies(
    joined: "Sequence[int]",
    left: "Sequence[int | None]",
    a_index: int,
    b_index: int,
    slice_at: int,
) -> bool:
    """The `overlap` population predicate for one adjacent pair (A, B): A's
    left_sim_time is non-NULL and B's boundary (its own left_sim_time, or
    slice_at when B is open) is at least 2 past B's joined_sim_time."""
    if left[a_index] is None:
        return False
    b_left = left[b_index]
    b_boundary = b_left if b_left is not None else slice_at
    return b_boundary - joined[b_index] >= 2


def _gap_row_qualifies(
    joined: "Sequence[int]", left: "Sequence[int | None]", index: int
) -> bool:
    """The `gap` population predicate for one closed row: left_sim_time is
    non-NULL and at least 2 past joined_sim_time."""
    left_value = left[index]
    if left_value is None:
        return False
    return left_value - joined[index] >= 2


def _left_before_join_row_qualifies(
    joined: "Sequence[int]", left: "Sequence[int | None]", index: int
) -> bool:
    """The `left_before_join` population predicate for one closed row:
    left_sim_time is non-NULL and strictly greater than joined_sim_time."""
    left_value = left[index]
    if left_value is None:
        return False
    return left_value > joined[index]


def enumerate_interval_units(
    mode: str,
    timelines: tuple[tuple[int, ...], ...],
    working: pa.Table,
    population_indices: "frozenset[int]",
    slice_at: int,
) -> tuple[tuple[int, int | None], ...]:
    """Enumerate one table's mode units over its member timelines.

    A unit is (mutated_row_index, successor_row_index) -- the successor is the
    pair's B for `overlap`, None for `gap` / `left_before_join`. Population
    filters per mode: `overlap` -- A.left_sim_time non-NULL and
    (B.left_sim_time or slice_at) - B.joined_sim_time >= 2; `gap` --
    left_sim_time non-NULL and left - joined >= 2; `left_before_join` --
    left_sim_time non-NULL and left > joined. A unit qualifies when its
    mutated row's index is in population_indices (the fork- and
    where-narrowed rows); adjacency always reads the full timelines. Units
    are ordered timeline-major, position-minor -- the canonical unit order.

    Args:
        mode: One of "overlap", "gap", "left_before_join".
        timelines: `enumerate_member_timelines`' output for `working`.
        working: The full working membership table the indices point into.
        population_indices: Row indices surviving the fork_path + where
            narrowing -- `where` decides unit membership, never adjacency.
        slice_at: The sole branch's slice boundary -- the open-interval
            successor bound for `overlap`.

    Returns:
        The ordered unit tuples; empty when no unit qualifies (a
        data-dependent no-op, never an error).

    Raises:
        CorruptError: `mode` is not one of the three literals -- an engine
            dispatch breach, not a config error (the discriminated union
            already rejected unknown modes at parse time).
    """
    if mode not in _MODES:
        raise CorruptError(f"distort_intervals: unknown mode {mode!r}")

    joined: list[int] = working.column("joined_sim_time").to_pylist()
    left: list[int | None] = working.column("left_sim_time").to_pylist()

    units: list[tuple[int, int | None]] = []
    for timeline in timelines:
        if mode == "overlap":
            for position in range(len(timeline) - 1):
                a_index = timeline[position]
                b_index = timeline[position + 1]
                if a_index not in population_indices:
                    continue
                if _overlap_pair_qualifies(joined, left, a_index, b_index, slice_at):
                    units.append((a_index, b_index))
        else:
            row_qualifies = (
                _gap_row_qualifies if mode == "gap" else _left_before_join_row_qualifies
            )
            for row_index in timeline:
                if row_index not in population_indices:
                    continue
                if row_qualifies(joined, left, row_index):
                    units.append((row_index, None))
    return tuple(units)


_TOUCHED_COLUMNS: dict[str, tuple[str, ...]] = {
    "overlap": ("left_sim_time",),
    "gap": ("left_sim_time",),
    "left_before_join": ("joined_sim_time", "left_sim_time"),
}
"""The timing column(s) each mode's rewrite touches -- overlap/gap change only
left_sim_time; left_before_join swaps both (DD § Invariants introduced #1)."""

_DEFECT_CLASSES: dict[str, str] = {
    "overlap": "overlapping_interval",
    "gap": "interval_gap",
    "left_before_join": "inverted_interval",
}

_IMPACTS: dict[str, tuple[str, ...]] = {
    "overlap": ("beyond-c1-c12",),
    "gap": ("beyond-c1-c12",),
    "left_before_join": ("C10",),
}
"""Each mode's unconditional declared impact -- closed by construction, per
DD § Defects, locators, and impact."""


class _Mutation(NamedTuple):
    """One performed timing rewrite: the pooled table it belongs to, the
    physical row touched, and its content before and after."""

    table_idx: int
    physical_row: int
    pre_row: dict[str, object]
    post_row: dict[str, object]


def _resolve_units_per_table(
    populations: "Sequence[TablePopulation]",
    mode: str,
    fork_path: str,
    slice_at: int,
) -> list[tuple[tuple[int, int | None], ...]]:
    """Each resolved table's mode units, over its full working table
    (fork-narrowed adjacency) and its where-narrowed population indices."""
    return [
        enumerate_interval_units(
            mode,
            enumerate_member_timelines(population.working_table.data, fork_path),
            population.working_table.data,
            frozenset(population.physical_indices),
            slice_at,
        )
        for population in populations
    ]


def _pool_units(
    units_per_table: "Sequence[tuple[tuple[int, int | None], ...]]",
) -> list[tuple[int, int]]:
    """Flatten per-table units into `(table_index, local_index)` pairs, in
    canonical table -> unit order -- the pooled order the draw indexes
    against."""
    return [
        (table_idx, local_index)
        for table_idx, units in enumerate(units_per_table)
        for local_index in range(len(units))
    ]


def _unit_weight_rows(
    working_table: "WorkingTable", units: tuple[tuple[int, int | None], ...]
) -> pa.Table:
    """One row per unit -- its earlier (mutated) row's content -- the
    population a placement weight derivation runs over (DD § Placement: a
    pair unit takes its earlier row's weight; a single-row unit its own)."""
    rows = [row_dict(working_table.data, mutated_index) for mutated_index, _ in units]
    return pa.Table.from_pylist(rows, schema=working_table.data.schema)


def _resolve_mutation(
    mode: str,
    table_idx: int,
    working_table: "WorkingTable",
    mutated_index: int,
    successor_index: int | None,
    slice_at: int,
) -> _Mutation | None:
    """Resolve one drawn unit's rewrite against the operation-start working
    state.

    Returns None for `overlap`'s no-mutation case (the rewrite target already
    equals the row's current value) -- never for `gap` / `left_before_join`,
    whose population filters guarantee a strict change (DD § The no-mutation
    rule).
    """
    pre_row = row_dict(working_table.data, mutated_index)
    post_row = dict(pre_row)
    if mode == "overlap":
        assert successor_index is not None
        b_row = row_dict(working_table.data, successor_index)
        b_joined = b_row["joined_sim_time"]
        b_left = b_row["left_sim_time"]
        assert isinstance(b_joined, int)
        b_end = b_left if b_left is not None else slice_at
        assert isinstance(b_end, int)
        new_left = b_joined + (b_end - b_joined) // 2
        if new_left == pre_row["left_sim_time"]:
            return None
        post_row["left_sim_time"] = new_left
    elif mode == "gap":
        joined = pre_row["joined_sim_time"]
        left = pre_row["left_sim_time"]
        assert isinstance(joined, int)
        assert isinstance(left, int)
        post_row["left_sim_time"] = joined + (left - joined) // 2
    else:
        assert mode == "left_before_join"
        post_row["joined_sim_time"] = pre_row["left_sim_time"]
        post_row["left_sim_time"] = pre_row["joined_sim_time"]
    return _Mutation(table_idx, mutated_index, pre_row, post_row)


def _build_overlays(
    mode: str,
    populations: "Sequence[TablePopulation]",
    mutations: "Sequence[_Mutation]",
) -> list[dict[str, list[object]]]:
    """One `{column: values}` full-column overlay per resolved table -- the
    write-back `write_back_pooled_columns` applies. A table with no counted
    mutation contributes an empty overlay (left untouched)."""
    columns = _TOUCHED_COLUMNS[mode]
    mutated_tables = {mutation.table_idx for mutation in mutations}
    overlays: list[dict[str, list[object]]] = [
        {
            column: population.working_table.data.column(column).to_pylist()
            for column in columns
        }
        if table_idx in mutated_tables
        else {}
        for table_idx, population in enumerate(populations)
    ]
    for mutation in mutations:
        overlay = overlays[mutation.table_idx]
        for column in columns:
            overlay[column][mutation.physical_row] = mutation.post_row[column]
    return overlays


def _build_defect(
    mutation: _Mutation,
    mode: str,
    table_name: str,
    spec: "TableSpec",
    rule: str,
) -> DefectRecord:
    """One DefectRecord for a counted mutation -- `left_before_join` at row
    granularity (carrying the post-corruption `joined_sim_time`), `overlap` /
    `gap` at cell granularity on `left_sim_time` (DD § Defects, locators, and
    impact)."""
    location: RowLocator | CellLocator
    if mode == "left_before_join":
        location = row_locator(table_name, "membership", spec, mutation.post_row)
    else:
        location = cell_locator(
            table_name, "membership", spec, mutation.post_row, "left_sim_time"
        )
    return DefectRecord.model_validate(
        {
            "class": _DEFECT_CLASSES[mode],
            "rule": rule,
            "impact": _IMPACTS[mode],
            "location": location,
        }
    )


class DistortIntervalsCorrupter:
    """Corrupter for `kind: distort_intervals` -- rewrites sampled membership
    intervals' timing boundaries: overlap an adjacent pair, open a coverage
    gap, or invert joined/left."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        """Apply one distort_intervals operation to the working set, in place.

        Resolves member timelines and the mode's unit population over the
        current working membership tables (fork-narrowed; `where` decides
        unit membership, never adjacency), pools units in canonical table
        order, draws from `rng` (placement setup then unit draw; the mode
        step draws nothing), rewrites the drawn units' timing cells as one
        simultaneous set resolved against the operation-start state, and
        returns the outcome with one DefectRecord per counted unit
        (`overlapping_interval` / `interval_gap` → cell locator on
        `left_sim_time`, impact `beyond-c1-c12`; `inverted_interval` → row
        locator, post-corruption coordinate, impact `C10`).

        See also `docs/architecture/corrupters.md` § distort_intervals: modes,
        populations, and rewrites, § What each operation breaks, and the
        impact it declares, § Placement: weights over units.

        Args:
            state: The shared working set; the touched entries are replaced.
            operation: The `DistortIntervals` config model.
            rule: The label stamped on each emitted DefectRecord.
            rng: The operation's deterministic RNG sub-stream.
            fork_path: The sole branch's fork_path.
            sidecar: The source sidecar -- `slice_at` (the open-interval
                boundary for `overlap`) and immutable table metadata; current
                schema is read from `WorkingTable.spec`.

        Returns:
            The outcome: units selected vs affected (the no-mutation rule may
            drop `overlap` units), plus the declared DefectRecords.

        Raises:
            CorruptError: A resolved working table lacks a structural
                membership column -- an engine-invariant breach, not a config
                error.
        """
        assert isinstance(operation, DistortIntervals)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        slice_at = branch_slice_at(sidecar, fork_path)
        units_per_table = _resolve_units_per_table(
            populations, operation.mode, fork_path, slice_at
        )
        pooled_units = _pool_units(units_per_table)

        if operation.placement is not None:
            weight_rows = [
                _unit_weight_rows(population.working_table, units)
                for population, units in zip(populations, units_per_table)
            ]
            weights_by_table = derive_row_weights(
                operation.placement,
                [
                    (population.working_table, rows)
                    for population, rows in zip(populations, weight_rows)
                ],
                rng,
            )
            weights = unit_row_weights(pooled_units, weights_by_table)
            drawn = draw_weighted_sample(weights, operation.amount, rng)
        else:
            drawn = draw_sample(len(pooled_units), operation.amount, rng)
        units_selected = len(drawn)
        if not drawn:
            return OperationOutcome(
                kind="distort_intervals",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        mutations: list[_Mutation] = []
        for pooled_index in sorted(drawn):
            table_idx, local_index = pooled_units[pooled_index]
            mutated_index, successor_index = units_per_table[table_idx][local_index]
            mutation = _resolve_mutation(
                operation.mode,
                table_idx,
                populations[table_idx].working_table,
                mutated_index,
                successor_index,
                slice_at,
            )
            if mutation is not None:
                mutations.append(mutation)

        if not mutations:
            return OperationOutcome(
                kind="distort_intervals",
                tables=tuple(table_names),
                units_selected=units_selected,
                units_affected=0,
                defects=(),
            )

        overlays = _build_overlays(operation.mode, populations, mutations)
        write_back_pooled_columns(state, populations, overlays)

        defects = tuple(
            _build_defect(
                mutation,
                operation.mode,
                populations[mutation.table_idx].table_name,
                populations[mutation.table_idx].working_table.spec,
                rule,
            )
            for mutation in mutations
        )

        return OperationOutcome(
            kind="distort_intervals",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(mutations),
            defects=defects,
        )
