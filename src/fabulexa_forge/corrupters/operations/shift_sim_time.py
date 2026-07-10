"""`shift_sim_time`: rewrite sampled history events' sim_time -- skew,
collide, or reorder.

See `docs/architecture/pending/corrupter-history-sequence-operations.md`
§ What each operation does, § The impact rule (normative) for the offset /
collide / swap semantics, the family no-mutation rule, the chained-swap skip
rule, and the anchor-participant impact rule this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pyarrow as pa

from fabulexa_forge.config.models import (
    ShiftCollide,
    ShiftOffset,
    ShiftSimTime,
    ShiftSwap,
)
from fabulexa_forge.corrupters.manifest import DefectRecord
from fabulexa_forge.corrupters.operations._impact import (
    SeriesKey,
    TablePopulation,
    branch_slice_at,
    draw_delta,
    enumerate_row_units,
    placement_populations,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    series_key,
    series_round_trip_fails,
    series_timeline,
    unit_row_weights,
)
from fabulexa_forge.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    resolve_target_tables,
)
from fabulexa_forge.corrupters.state import OperationOutcome, WorkingTable

if TYPE_CHECKING:
    import random

    from fabulexa_forge.config.models import CorruptOperation
    from fabulexa_forge.corrupters.manifest import ImpactCode
    from fabulexa_forge.corrupters.state import CorruptState
    from fabulexa_forge.reader.sidecar import Sidecar


class _Mutation(NamedTuple):
    """One performed sim_time rewrite: the physical row touched, its content
    before and after, the series it belongs to, and the defect class its
    record carries."""

    physical_row: int
    pre_row: dict[str, object]
    post_row: dict[str, object]
    series: SeriesKey
    defect_class: str


def _cached_timeline(
    cache: dict[SeriesKey, tuple[pa.Table, list[int]]],
    history_table: "WorkingTable",
    fork_path: str,
    key: SeriesKey,
) -> tuple[pa.Table, list[int]]:
    """`series_timeline`, memoized per series key for one operation's apply."""
    if key not in cache:
        kind, property_name, record_id = key
        cache[key] = series_timeline(
            history_table, fork_path, kind, record_id, property_name
        )
    return cache[key]


def _predecessor_tick(timeline_sim_times: list[int], sim_time: int) -> int | None:
    """The greatest tick strictly less than `sim_time` among
    `timeline_sim_times`, or None when `sim_time` is the series' minimum
    (tied minima included)."""
    candidates = [t for t in timeline_sim_times if t < sim_time]
    return max(candidates) if candidates else None


def _has_predecessor(
    history_table: "WorkingTable",
    fork_path: str,
    row: dict[str, object],
    cache: dict[SeriesKey, tuple[pa.Table, list[int]]],
) -> bool:
    """Whether `row`'s series carries a strict predecessor tick -- the
    `collide` / `swap` population exclusion (§ Populations and units)."""
    timeline, _indices = _cached_timeline(
        cache, history_table, fork_path, series_key(row)
    )
    sim_time = row["sim_time"]
    assert isinstance(sim_time, int)
    sim_times = timeline.column("sim_time").to_pylist()
    return _predecessor_tick(sim_times, sim_time) is not None


def _filter_predecessor_population(
    history_table: "WorkingTable",
    fork_path: str,
    population: TablePopulation,
    cache: dict[SeriesKey, tuple[pa.Table, list[int]]],
) -> TablePopulation:
    """Narrow `population` to rows whose series carries a strict predecessor
    tick -- the `collide` / `swap` population (§ Populations and units)."""
    keep_positions = [
        row_pos
        for row_pos in range(population.content.num_rows)
        if _has_predecessor(
            history_table, fork_path, row_dict(population.content, row_pos), cache
        )
    ]
    content = population.content.take(pa.array(keep_positions, type=pa.int64()))
    physical_indices = [population.physical_indices[pos] for pos in keep_positions]
    return TablePopulation(
        table_name=population.table_name,
        working_table=population.working_table,
        content=content,
        physical_indices=physical_indices,
    )


def _resolve_offset(
    shift: ShiftOffset,
    selected_rows: list[tuple[int, dict[str, object]]],
    rng: "random.Random",
) -> list[_Mutation]:
    """One delta per selected event, in ascending selected-unit order,
    round-half-to-even to BIGINT. A zero-rounded delta leaves the row
    unchanged -- no mutation is recorded, but the draw is still consumed."""
    mutations: list[_Mutation] = []
    for physical_row, row in selected_rows:
        delta = draw_delta(shift.distribution, rng)
        rounded = round(delta)
        if rounded == 0:
            continue
        sim_time = row["sim_time"]
        assert isinstance(sim_time, int)
        post_row = dict(row)
        post_row["sim_time"] = sim_time + rounded
        mutations.append(
            _Mutation(
                physical_row, row, post_row, series_key(row), "shifted_event_time"
            )
        )
    return mutations


def _resolve_collide(
    history_table: "WorkingTable",
    fork_path: str,
    selected_rows: list[tuple[int, dict[str, object]]],
    cache: dict[SeriesKey, tuple[pa.Table, list[int]]],
) -> list[_Mutation]:
    """Every selected event's sim_time becomes its predecessor tick -- always
    a real mutation (the predecessor tick is strictly less than the event's
    own tick, per the population's pre-filter)."""
    mutations: list[_Mutation] = []
    for physical_row, row in selected_rows:
        key = series_key(row)
        timeline, _indices = _cached_timeline(cache, history_table, fork_path, key)
        sim_time = row["sim_time"]
        assert isinstance(sim_time, int)
        sim_times = timeline.column("sim_time").to_pylist()
        predecessor = _predecessor_tick(sim_times, sim_time)
        assert predecessor is not None  # the population was pre-filtered
        post_row = dict(row)
        post_row["sim_time"] = predecessor
        mutations.append(_Mutation(physical_row, row, post_row, key, "tick_collision"))
    return mutations


def _resolve_swap(
    history_table: "WorkingTable",
    fork_path: str,
    selected_rows: list[tuple[int, dict[str, object]]],
    cache: dict[SeriesKey, tuple[pa.Table, list[int]]],
) -> list[_Mutation]:
    """Exchange each selected event's sim_time with its predecessor-tick
    partner's, in ascending selected-unit order. An equal-value pair (the
    multiset is unchanged) or a chained pair (either row already rewritten
    this operation) is skipped -- no rewrite, no defects, per the family
    no-mutation rule."""
    mutations: list[_Mutation] = []
    used_physical_rows: set[int] = set()
    for physical_row, row in selected_rows:
        key = series_key(row)
        timeline, indices = _cached_timeline(cache, history_table, fork_path, key)
        sim_time = row["sim_time"]
        assert isinstance(sim_time, int)
        sim_times = timeline.column("sim_time").to_pylist()
        predecessor = _predecessor_tick(sim_times, sim_time)
        assert predecessor is not None  # the population was pre-filtered
        partner_pos = sim_times.index(predecessor)
        partner_physical_row = indices[partner_pos]
        if (
            physical_row in used_physical_rows
            or partner_physical_row in used_physical_rows
        ):
            continue  # chained swap: skip
        partner_row = row_dict(timeline, partner_pos)
        if row["value"] == partner_row["value"]:
            continue  # equal-value swap: no rewrite

        event_post = dict(row)
        event_post["sim_time"] = partner_row["sim_time"]
        partner_post = dict(partner_row)
        partner_post["sim_time"] = sim_time

        mutations.append(
            _Mutation(physical_row, row, event_post, key, "reordered_event")
        )
        mutations.append(
            _Mutation(
                partner_physical_row, partner_row, partner_post, key, "reordered_event"
            )
        )
        used_physical_rows.add(physical_row)
        used_physical_rows.add(partner_physical_row)
    return mutations


def _apply_mutations(
    history_table: "WorkingTable", mutations: list[_Mutation]
) -> WorkingTable:
    """Apply every mutation's sim_time rewrite as one atomic set (§
    Simultaneous rewrite). Builds the whole rewritten `sim_time` column
    before replacing it, so a BIGINT-overflowing sum fails loudly (an Arrow
    OverflowError) before any table is written -- never a silent wrap."""
    sim_times = history_table.data.column("sim_time").to_pylist()
    for mutation in mutations:
        new_sim_time = mutation.post_row["sim_time"]
        assert isinstance(new_sim_time, int)
        sim_times[mutation.physical_row] = new_sim_time
    field_index = history_table.data.schema.get_field_index("sim_time")
    new_data = history_table.data.set_column(
        field_index, "sim_time", pa.array(sim_times, type=pa.int64())
    )
    return WorkingTable(spec=history_table.spec, data=new_data)


def _shift_impact(
    mutation: _Mutation,
    pre_anchor: dict[SeriesKey, tuple[int, str] | None],
    post_anchor: dict[SeriesKey, tuple[int, str] | None],
    round_trip_fails: dict[SeriesKey, bool],
) -> tuple["ImpactCode", ...]:
    """The anchor-participant impact rule for one shift_sim_time mutation (§
    The impact rule -- the anchor-participant rule): a moved row participates
    if it *was* its series' anchor before this operation or *becomes* it
    after. `C6` iff a participant and the post-operation round-trip fails;
    `beyond-c1-c12` otherwise."""
    pre_pair = (mutation.pre_row["sim_time"], mutation.pre_row["value"])
    post_pair = (mutation.post_row["sim_time"], mutation.post_row["value"])
    participant = (
        pre_pair == pre_anchor[mutation.series]
        or post_pair == post_anchor[mutation.series]
    )
    if participant and round_trip_fails[mutation.series]:
        return ("C6",)
    return ("beyond-c1-c12",)


class ShiftSimTimeCorrupter:
    """Corrupter for `kind: shift_sim_time` -- rewrites sampled history
    events' sim_time: additive skew (offset), predecessor-tick collision
    (collide), or a predecessor-tick value exchange (swap)."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, ShiftSimTime)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        history_population = populations[0]
        history_table = history_population.working_table
        shift = operation.shift
        timeline_cache: dict[SeriesKey, tuple[pa.Table, list[int]]] = {}

        if isinstance(shift, ShiftOffset):
            unit_population = history_population
        else:
            unit_population = _filter_predecessor_population(
                history_table, fork_path, history_population, timeline_cache
            )

        row_units = enumerate_row_units([unit_population], [True])
        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations([unit_population]), rng
            )
            weights = unit_row_weights(row_units, row_weights)
            drawn = draw_weighted_sample(weights, operation.amount, rng)
        else:
            drawn = draw_sample(len(row_units), operation.amount, rng)
        units_selected = len(drawn)
        if not drawn:
            return OperationOutcome(
                kind="shift_sim_time",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        selected_rows = [
            (
                unit_population.physical_indices[row_units[i][1]],
                row_dict(unit_population.content, row_units[i][1]),
            )
            for i in sorted(drawn)
        ]

        if isinstance(shift, ShiftOffset):
            mutations = _resolve_offset(shift, selected_rows, rng)
        elif isinstance(shift, ShiftCollide):
            mutations = _resolve_collide(
                history_table, fork_path, selected_rows, timeline_cache
            )
        else:
            assert isinstance(shift, ShiftSwap)
            mutations = _resolve_swap(
                history_table, fork_path, selected_rows, timeline_cache
            )

        if not mutations:
            return OperationOutcome(
                kind="shift_sim_time",
                tables=tuple(table_names),
                units_selected=units_selected,
                units_affected=0,
                defects=(),
            )

        slice_at = branch_slice_at(sidecar, fork_path)
        involved = {mutation.series for mutation in mutations}
        pre_anchor = {
            key: resolve_c6_anchor(history_table.data, fork_path, slice_at, *key)
            for key in involved
        }

        new_working_table = _apply_mutations(history_table, mutations)
        state.tables["history"] = new_working_table

        post_anchor = {
            key: resolve_c6_anchor(new_working_table.data, fork_path, slice_at, *key)
            for key in involved
        }
        round_trip_fails = {
            key: series_round_trip_fails(state, fork_path, slice_at, *key)
            for key in involved
        }

        row_category = row_category_for_table(history_table.spec)
        defects: list[DefectRecord] = []
        for mutation in mutations:
            impact = _shift_impact(mutation, pre_anchor, post_anchor, round_trip_fails)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": mutation.defect_class,
                        "rule": rule,
                        "impact": impact,
                        "location": row_locator(
                            history_population.table_name,
                            row_category,
                            history_table.spec,
                            mutation.post_row,
                        ),
                    }
                )
            )

        units_affected = (
            len(mutations) // 2 if isinstance(shift, ShiftSwap) else len(mutations)
        )

        return OperationOutcome(
            kind="shift_sim_time",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=units_affected,
            defects=tuple(defects),
        )
