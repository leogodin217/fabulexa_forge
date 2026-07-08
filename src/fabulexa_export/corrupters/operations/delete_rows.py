"""`delete_rows`: row removal from records / membership tables -- the wake.

See `docs/architecture/corrupters.md` § `delete_rows` for the population/unit,
tombstone, and wake-impact rule this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_export.config.models import DeleteRows
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    branch_slice_at,
    enumerate_row_units,
    is_pinned_record_id,
    membership_kind_id_pairs,
    placement_populations,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    series_round_trip_fails,
    unit_row_weights,
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
    from collections.abc import Sequence

    from fabulexa_export.config.models import CorruptOperation
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.operations._impact import TablePopulation
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import Sidecar

_SelectedRow = tuple[int, int, dict[str, object]]
"""One drawn row: `(table_index, physical_row, row)`, in canonical pooled order."""


def _select_rows(
    populations: "Sequence[TablePopulation]",
    row_units: "Sequence[tuple[int, int]]",
    drawn: "Sequence[int]",
) -> list[_SelectedRow]:
    """The drawn rows, resolved to their table index, physical index, and
    pre-removal content, in canonical pooled order.

    Args:
        populations: The operation's resolved-table populations, canonical
            table order.
        row_units: The pooled `(table_index, row_pos)` row units, canonical
            order.
        drawn: The sampled unit indices into `row_units`.

    Returns:
        One entry per drawn unit, canonical-order (ascending unit index).
    """
    selected: list[_SelectedRow] = []
    for unit_index in sorted(drawn):
        table_idx, row_pos = row_units[unit_index]
        population = populations[table_idx]
        physical_row = population.physical_indices[row_pos]
        row = row_dict(population.content, row_pos)
        selected.append((table_idx, physical_row, row))
    return selected


def _record_tombstones(
    state: "CorruptState",
    populations: "Sequence[TablePopulation]",
    selected: "Sequence[_SelectedRow]",
) -> None:
    """Write each removed `records__<K>` row's `record_id` into
    `state.deleted_record_ids[K]`. Membership removals record nothing.

    Args:
        state: The working set, mutated in place.
        populations: The operation's resolved-table populations, canonical
            table order.
        selected: The drawn rows, canonical pooled order.
    """
    for table_idx, _physical_row, row in selected:
        table_spec = populations[table_idx].working_table.spec
        if table_spec.category != "records":
            continue
        kind = table_spec.record_kind
        assert kind is not None
        record_id = row["record_id"]
        assert isinstance(record_id, str)
        state.deleted_record_ids.setdefault(kind, set()).add(record_id)


def _remove_selected_rows(
    state: "CorruptState",
    populations: "Sequence[TablePopulation]",
    selected: "Sequence[_SelectedRow]",
) -> None:
    """Remove every drawn row from its working table, as one simultaneous set
    per table.

    Args:
        state: The working set, mutated in place.
        populations: The operation's resolved-table populations, canonical
            table order.
        selected: The drawn rows, canonical pooled order.
    """
    drop_positions_by_table: dict[int, set[int]] = {}
    for table_idx, physical_row, _row in selected:
        drop_positions_by_table.setdefault(table_idx, set()).add(physical_row)
    for table_idx, positions in drop_positions_by_table.items():
        working_table = populations[table_idx].working_table
        keep_indices = [
            i for i in range(working_table.data.num_rows) if i not in positions
        ]
        new_data = working_table.data.take(pa.array(keep_indices, type=pa.int64()))
        state.tables[populations[table_idx].table_name] = WorkingTable(
            spec=working_table.spec, data=new_data
        )


def _history_properties(
    state: "CorruptState", fork_path: str, kind: str, record_id: str
) -> set[str]:
    """Every distinct `property` the working `history` table carries for
    `(fork_path, kind, record_id)`.

    Args:
        state: The working set, as of the point this is called.
        fork_path: The sole branch's fork_path.
        kind: The series' record kind.
        record_id: The series' record id.

    Returns:
        The distinct `property` values of matching working `history` rows.
    """
    history_working = state.tables.get("history")
    if history_working is None:
        return set()
    data = history_working.data
    fork_paths = data.column("fork_path")
    kinds = data.column("kind")
    record_ids = data.column("record_id")
    properties = data.column("property")
    result: set[str] = set()
    for i in range(data.num_rows):
        if (
            fork_paths[i].as_py() == fork_path
            and kinds[i].as_py() == kind
            and record_ids[i].as_py() == record_id
        ):
            value = properties[i].as_py()
            assert isinstance(value, str)
            result.add(value)
    return result


def _delete_wake_impact(
    state: "CorruptState",
    sidecar: "Sidecar",
    fork_path: str,
    slice_at: int,
    table_name: str,
    kind: str,
    record_id: str,
    membership_pairs: frozenset[tuple[str, str]],
) -> tuple["ImpactCode", ...]:
    """The wake impact for one deleted `records__<K>` row, evaluated against
    the post-operation working state (design doc § Semantics, `delete_rows` --
    the wake).

    Args:
        state: The working set, after this operation's removals.
        sidecar: The source sidecar, for the pinned-id lookup.
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at.
        table_name: The deleted row's `records__<K>` table name.
        kind: The deleted row's record kind (`K`).
        record_id: The deleted row's record id (`R`).
        membership_pairs: Every `(kind, record_id)` pair a surviving
            membership row resolves to, post-operation.

    Returns:
        The union of tripped codes, or the lone `beyond-c1-c12` sentinel when
        the union is empty.
    """
    records_working = state.tables[table_name]
    survivor_ids = set(records_working.data.column("record_id").to_pylist())
    zero_survivors = record_id not in survivor_ids
    codes: set["ImpactCode"] = set()
    if (
        zero_survivors
        and records_working.data.num_rows > 0
        and is_pinned_record_id(sidecar, kind, record_id)
    ):
        codes.add("C9")
    if zero_survivors:
        for property_name in _history_properties(state, fork_path, kind, record_id):
            if series_round_trip_fails(
                state, fork_path, slice_at, kind, property_name, record_id
            ):
                codes.add("C6")
                break
    if zero_survivors and (kind, record_id) in membership_pairs:
        codes.add("C10")
    if not codes:
        return ("beyond-c1-c12",)
    return tuple(sorted(codes))


def _build_defects(
    state: "CorruptState",
    sidecar: "Sidecar",
    fork_path: str,
    populations: "Sequence[TablePopulation]",
    selected: "Sequence[_SelectedRow]",
    rule: str,
) -> list[DefectRecord]:
    """One `deleted_row` defect per removed row, source-coordinate locator,
    wake impact evaluated against the post-operation state.

    Args:
        state: The working set, after this operation's removals.
        sidecar: The source sidecar, for the wake's pinned-id lookup.
        fork_path: The sole branch's fork_path.
        populations: The operation's resolved-table populations, canonical
            table order.
        selected: The drawn rows, canonical pooled order.
        rule: The label to stamp on each emitted DefectRecord.

    Returns:
        One `DefectRecord` per entry of `selected`, in the same order.
    """
    slice_at = branch_slice_at(sidecar, fork_path)
    membership_pairs = membership_kind_id_pairs(state)
    defects: list[DefectRecord] = []
    for table_idx, _physical_row, row in selected:
        population = populations[table_idx]
        table_spec = population.working_table.spec
        row_category = row_category_for_table(table_spec)
        if table_spec.category == "records":
            kind = table_spec.record_kind
            assert kind is not None
            record_id = row["record_id"]
            assert isinstance(record_id, str)
            impact = _delete_wake_impact(
                state,
                sidecar,
                fork_path,
                slice_at,
                population.table_name,
                kind,
                record_id,
                membership_pairs,
            )
        else:
            impact = ("beyond-c1-c12",)
        defects.append(
            DefectRecord.model_validate(
                {
                    "class": "deleted_row",
                    "rule": rule,
                    "impact": impact,
                    "location": row_locator(
                        population.table_name, row_category, table_spec, row
                    ),
                }
            )
        )
    return defects


class DeleteRowsCorrupter:
    """Corrupter for `kind: delete_rows` -- removes sampled rows from records
    / membership tables, declaring each removal's wake impact."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, DeleteRows)
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
                kind="delete_rows",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        selected = _select_rows(populations, row_units, drawn)
        _record_tombstones(state, populations, selected)
        _remove_selected_rows(state, populations, selected)
        defects = _build_defects(state, sidecar, fork_path, populations, selected, rule)

        return OperationOutcome(
            kind="delete_rows",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
