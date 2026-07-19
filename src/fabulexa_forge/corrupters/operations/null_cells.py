"""`null_cells`: missing-value injection over a sampled set of value cells.

See `docs/architecture/pending/corrupter-engine-and-manifest.md` § What each
operation breaks, § C7 groups are structural (normative) for the impact rule
this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_forge.config.models import NullCells
from fabulexa_forge.corrupters.manifest import DefectRecord
from fabulexa_forge.corrupters.operations._impact import (
    cell_locator,
    current_value,
    enumerate_cell_units,
    history_series_exists,
    is_deactivated_at_column,
    is_membership_ref_column,
    is_round_trippable_type,
    membership_partner_column,
    placement_populations,
    property_name_for_prop_column,
    records_reference_sibling,
    resolve_pooled_populations,
    row_category_for_table,
    unit_row_weights,
    write_back_pooled_columns,
)
from fabulexa_forge.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    match_column_entries,
    resolve_target_tables,
)
from fabulexa_forge.corrupters.state import OperationOutcome
from fabulexa_forge.corrupters.validate import is_nullable_column

if TYPE_CHECKING:
    import random

    from fabulexa_forge.config.models import CorruptOperation
    from fabulexa_forge.corrupters.manifest import ImpactCode
    from fabulexa_forge.corrupters.state import CorruptState
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar, TableSpec


def _cell_impact(
    column: str,
    column_spec: "ColumnSpec",
    table_spec: "TableSpec",
    fork_path: str,
    record_id: str,
    history_data: pa.Table | None,
    py_columns: dict[str, list[object]],
    content: pa.Table,
    row_pos: int,
    physical_row: int,
) -> tuple["ImpactCode", ...]:
    """The impact rule for one nulled cell, against the current working state.

    records `prop__`: C6 iff round-trippable-typed and its (kind, record_id,
    property) has a history series. Membership `member__<f>__kind`/`__id`: C7
    iff the null leaves the partner cell non-NULL (partly populated);
    beyond-c1-c12 iff the partner is also NULL (completes an all-NULL pair,
    healing C7). `deactivated_at`: always C7 (a non-NULL value marks an
    inactive row; nulling it violates NULL-iff-active). Any other value
    column: beyond-c1-c12.
    """
    if column.startswith("prop__"):
        if (
            column_spec.history_tracked
            and is_round_trippable_type(column_spec.type)
            and table_spec.record_kind is not None
            and history_series_exists(
                history_data,
                fork_path,
                table_spec.record_kind,
                record_id,
                property_name_for_prop_column(column),
            )
        ):
            return ("C6",)
        return ("beyond-c1-c12",)
    if is_membership_ref_column(column):
        partner = membership_partner_column(column)
        partner_value = current_value(
            py_columns, content, row_pos, physical_row, partner
        )
        if partner_value is None:
            return ("beyond-c1-c12",)
        return ("C7",)
    if is_deactivated_at_column(column):
        return ("C7",)
    return ("beyond-c1-c12",)


class NullCellsCorrupter:
    """Corrupter for `kind: null_cells` — nulls a sampled set of value cells."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, NullCells)
        entries = operation.target.columns
        assert entries is not None
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        per_table_columns = [
            match_column_entries(
                entries,
                [
                    col.name
                    for col in population.working_table.spec.columns
                    if is_nullable_column(col.name)
                ],
            )
            for population in populations
        ]
        pooled_units = enumerate_cell_units(populations, per_table_columns)
        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations(populations), rng
            )
            weights = unit_row_weights(pooled_units, row_weights)
            drawn = draw_weighted_sample(weights, operation.amount, rng)
        else:
            drawn = draw_sample(len(pooled_units), operation.amount, rng)
        units_selected = len(drawn)

        history_working = state.tables.get("history")
        history_data = history_working.data if history_working is not None else None

        py_columns_by_table: list[dict[str, list[object]]] = [{} for _ in populations]
        defects: list[DefectRecord] = []
        for unit_index in sorted(drawn):
            table_idx, row_pos, column = pooled_units[unit_index]
            population = populations[table_idx]
            content = population.content
            table_spec = population.working_table.spec
            columns_by_name = {col.name: col for col in table_spec.columns}
            py_columns = py_columns_by_table[table_idx]
            physical_row = population.physical_indices[row_pos]
            current = current_value(py_columns, content, row_pos, physical_row, column)
            if current is None:
                continue

            record_id = content.column("record_id")[row_pos].as_py()
            assert isinstance(record_id, str)
            impact = _cell_impact(
                column,
                columns_by_name[column],
                table_spec,
                fork_path,
                record_id,
                history_data,
                py_columns,
                content,
                row_pos,
                physical_row,
            )
            row = {
                name: current_value(py_columns, content, row_pos, physical_row, name)
                for name in content.schema.names
            }
            row_category = row_category_for_table(table_spec)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "missing_value",
                        "rule": rule,
                        "impact": impact,
                        "location": cell_locator(
                            population.table_name, row_category, table_spec, row, column
                        ),
                    }
                )
            )
            if column not in py_columns:
                py_columns[column] = population.working_table.data.column(
                    column
                ).to_pylist()
            py_columns[column][physical_row] = None

            sibling = records_reference_sibling(column, columns_by_name[column])
            if sibling is not None:
                if sibling not in py_columns:
                    py_columns[sibling] = population.working_table.data.column(
                        sibling
                    ).to_pylist()
                py_columns[sibling][physical_row] = None

        write_back_pooled_columns(state, populations, py_columns_by_table)

        return OperationOutcome(
            kind="null_cells",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
