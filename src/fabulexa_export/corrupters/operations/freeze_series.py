"""`freeze_series`: suppress a series' timeline tail so its value sticks.

See `docs/architecture/pending/corrupter-history-sequence-operations.md`
§ What each operation does, § Placement over the new units, § The impact rule
(normative) for the cut semantics, the terminal-row placement-weight rule, and
the anchor-participant impact rule this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_export.config.models import FreezeSeries
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    SeriesKey,
    anchor_participant_impact,
    branch_slice_at,
    enumerate_series_units,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    series_key,
    series_round_trip_fails,
    series_timeline,
)
from fabulexa_export.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    resolve_target_tables,
)
from fabulexa_export.corrupters.state import OperationOutcome, WorkingTable

if TYPE_CHECKING:
    import random

    from fabulexa_export.config.models import CorruptOperation
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import Sidecar

_SeriesUnit = tuple[str, str, str]
"""A series unit key, `(kind, record_id, property)` -- `enumerate_series_units`'
tuple order, the canonical order the series-unit draw, placement weights, and
mode draws all index against. Scoped to population/enumeration bookkeeping
only; `_series_key` is the sole boundary that reorders a unit into the
shared `SeriesKey` order (`_impact.py`) for the anchor/round-trip calls."""


def _series_key(unit: _SeriesUnit) -> SeriesKey:
    """Reorder an `enumerate_series_units`-order unit `(kind, record_id,
    property)` into the shared `SeriesKey` order `(kind, property,
    record_id)` -- the sole place the two orders cross, so no call site
    positionally unpacks one order into the other."""
    kind, record_id, property_name = unit
    return (kind, property_name, record_id)


def _series_terminal_rows(
    history_table: "WorkingTable", fork_path: str, units: tuple[_SeriesUnit, ...]
) -> pa.Table:
    """Each series' terminal row, one per `units`, in `units`' order -- the
    population a placement weight derivation runs over (§ Placement over the
    new units). A series' terminal row is its timeline's last row: the
    timeline's canonical order already ranks by `(sim_time DESC, value DESC)`
    at the maximal `sim_time`.

    Args:
        history_table: The working `history` table, as of the operation's
            start.
        fork_path: The sole branch's fork_path.
        units: The series universe, in canonical series order.

    Returns:
        One row per `units` entry, `history_table`'s schema, `units`' order.
    """
    rows: list[dict[str, object]] = []
    for unit in units:
        kind, record_id, property_name = unit
        timeline, _indices = series_timeline(
            history_table, fork_path, kind, record_id, property_name
        )
        rows.append(row_dict(timeline, timeline.num_rows - 1))
    return pa.Table.from_pylist(rows, schema=history_table.data.schema)


class FreezeSeriesCorrupter:
    """Corrupter for `kind: freeze_series` -- suppresses each selected
    series' timeline tail so its value sticks."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, FreezeSeries)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        history_population = populations[0]
        history_table = history_population.working_table

        series_units = enumerate_series_units(
            history_population.content, history_table.data, fork_path
        )
        if operation.placement is not None:
            terminal_rows = _series_terminal_rows(
                history_table, fork_path, series_units
            )
            weights_by_table = derive_row_weights(
                operation.placement, [(history_table, terminal_rows)], rng
            )
            drawn = draw_weighted_sample(weights_by_table[0], operation.amount, rng)
        else:
            drawn = draw_sample(len(series_units), operation.amount, rng)
        units_selected = len(drawn)
        if not drawn:
            return OperationOutcome(
                kind="freeze_series",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        slice_at = branch_slice_at(sidecar, fork_path)
        selected_units = [series_units[i] for i in sorted(drawn)]
        selected_keys = [_series_key(unit) for unit in selected_units]

        pre_anchor: dict[SeriesKey, tuple[int, str] | None] = {}
        timelines: dict[SeriesKey, tuple[pa.Table, list[int]]] = {}
        for unit, key in zip(selected_units, selected_keys):
            kind, record_id, property_name = unit
            pre_anchor[key] = resolve_c6_anchor(
                history_table.data, fork_path, slice_at, *key
            )
            timelines[key] = series_timeline(
                history_table, fork_path, kind, record_id, property_name
            )

        removed: list[tuple[int, dict[str, object]]] = []
        for key in selected_keys:
            timeline, indices = timelines[key]
            n = timeline.num_rows
            cut = 1 if operation.cut == "after_first" else rng.randrange(1, n)
            for row_pos in range(cut, n):
                removed.append((indices[row_pos], row_dict(timeline, row_pos)))

        remove_positions = {physical_row for physical_row, _row in removed}
        keep_indices = [
            i for i in range(history_table.data.num_rows) if i not in remove_positions
        ]
        new_data = history_table.data.take(pa.array(keep_indices, type=pa.int64()))
        state.tables["history"] = WorkingTable(spec=history_table.spec, data=new_data)

        round_trip_fails = {
            key: series_round_trip_fails(state, fork_path, slice_at, *key)
            for key in selected_keys
        }

        row_category = row_category_for_table(history_table.spec)
        defects: list[DefectRecord] = []
        for _physical_row, row in removed:
            key = series_key(row)
            impact = anchor_participant_impact(
                row, pre_anchor[key], round_trip_fails[key]
            )
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "frozen_series_event",
                        "rule": rule,
                        "impact": impact,
                        "location": row_locator(
                            history_population.table_name,
                            row_category,
                            history_table.spec,
                            row,
                        ),
                    }
                )
            )

        return OperationOutcome(
            kind="freeze_series",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=units_selected,
            defects=tuple(defects),
        )
