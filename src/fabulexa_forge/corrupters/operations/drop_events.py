"""`drop_events`: history event row removal — lost CDC messages.

See `docs/architecture/pending/corrupter-history-sequence-operations.md`
§ What each operation does, § The impact rule (normative) for the
source-coordinate locator stance and the anchor-participant impact rule this
handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_forge.config.models import DropEvents
from fabulexa_forge.corrupters.manifest import DefectRecord
from fabulexa_forge.corrupters.operations._impact import (
    SeriesKey,
    anchor_participant_impact,
    branch_slice_at,
    enumerate_row_units,
    placement_populations,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    series_key,
    series_round_trip_fails,
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
    from fabulexa_forge.corrupters.state import CorruptState
    from fabulexa_forge.reader.sidecar import Sidecar


class DropEventsCorrupter:
    """Corrupter for `kind: drop_events` — removes sampled history event rows."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, DropEvents)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        row_units = enumerate_row_units(populations, [True for _ in populations])
        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations(populations), rng
            )
            weights = unit_row_weights(row_units, row_weights)
            drawn = draw_weighted_sample(weights, operation.amount, rng)
        else:
            drawn = draw_sample(len(row_units), operation.amount, rng)
        units_selected = len(drawn)
        if not drawn:
            return OperationOutcome(
                kind="drop_events",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        history_population = populations[0]
        history_table = history_population.working_table
        slice_at = branch_slice_at(sidecar, fork_path)

        selected: list[tuple[int, dict[str, object]]] = []
        series_keys: set[SeriesKey] = set()
        for unit_index in sorted(drawn):
            table_idx, row_pos = row_units[unit_index]
            population = populations[table_idx]
            physical_row = population.physical_indices[row_pos]
            row = row_dict(population.content, row_pos)
            selected.append((physical_row, row))
            series_keys.add(series_key(row))

        pre_anchor = {
            key: resolve_c6_anchor(history_table.data, fork_path, slice_at, *key)
            for key in series_keys
        }

        drop_positions = {physical_row for physical_row, _row in selected}
        keep_indices = [
            i for i in range(history_table.data.num_rows) if i not in drop_positions
        ]
        new_data = history_table.data.take(pa.array(keep_indices, type=pa.int64()))
        state.tables["history"] = WorkingTable(spec=history_table.spec, data=new_data)

        round_trip_fails = {
            key: series_round_trip_fails(state, fork_path, slice_at, *key)
            for key in series_keys
        }

        row_category = row_category_for_table(history_table.spec)
        defects: list[DefectRecord] = []
        for _physical_row, row in selected:
            key = series_key(row)
            impact = anchor_participant_impact(
                row, pre_anchor[key], round_trip_fails[key]
            )
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "dropped_event",
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
            kind="drop_events",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
