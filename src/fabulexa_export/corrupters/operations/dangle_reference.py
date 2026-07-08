"""`dangle_reference`: referential breakage over a sampled set of reference cells.

See `docs/architecture/pending/corrupter-engine-and-manifest.md` § What each
operation breaks, § `dangle_reference` rewrites a sampled reference id...
(normative) for the three-filter population, per-row target-kind resolution,
and sentinel-value rule this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_export.config.models import DangleReference
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    cell_locator,
    enumerate_cell_units,
    history_series_exists,
    is_membership_id_column,
    membership_partner_column,
    placement_populations,
    property_name_for_prop_column,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    unit_row_weights,
    write_back_pooled_columns,
)
from fabulexa_export.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    match_column_entries,
    resolve_target_tables,
)
from fabulexa_export.corrupters.state import OperationOutcome
from fabulexa_export.corrupters.validate import is_reference_column

if TYPE_CHECKING:
    import random

    from fabulexa_export.config.models import CorruptOperation
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar, TableSpec

DANGLING_ID_PREFIX = "__dangling__"
"""Fixed sentinel prefix a dangled reference id is rewritten to."""


def _resolve_target_kind(
    column: str, col_spec: "ColumnSpec", content: pa.Table, row_pos: int
) -> str | None:
    """The reference target kind for one row's cell, or None to filter it out.

    Population filters (1) and (2): None when the id itself is NULL (an
    absent reference cannot be dangled), or — for a membership id column —
    when its partner `member__<f>__kind` is also NULL (an earlier null_cells
    may have emptied it; the target kind is then unknown).

    Args:
        column: The reference column's current name.
        col_spec: The column's current ColumnSpec.
        content: The canonically-ordered population content.
        row_pos: The row's 0-based canonical-order position.

    Returns:
        The row's target records kind, or None when population-filtered.
    """
    id_value = content.column(column)[row_pos].as_py()
    if id_value is None:
        return None
    if is_membership_id_column(column):
        partner = membership_partner_column(column)
        kind_value = content.column(partner)[row_pos].as_py()
        if kind_value is None:
            return None
        assert isinstance(kind_value, str)
        return kind_value
    assert col_spec.references is not None
    return col_spec.references


def _working_id_values(state: "CorruptState", kind: str) -> set[str]:
    """Every `record_id` currently in the working `records__<kind>` table."""
    target = state.tables[f"records__{kind}"]
    return set(target.data.column("record_id").to_pylist())


def _smallest_absent_sentinel(taken: set[str]) -> str:
    """The smallest-suffix `__dangling__<n>` id absent from `taken`."""
    n = 0
    while f"{DANGLING_ID_PREFIX}{n}" in taken:
        n += 1
    return f"{DANGLING_ID_PREFIX}{n}"


def _dangle_impact(
    column: str,
    col_spec: "ColumnSpec",
    table_spec: "TableSpec",
    fork_path: str,
    record_id: str,
    history_data: "pa.Table | None",
) -> tuple["ImpactCode", ...]:
    """The impact rule for one dangled reference cell.

    A membership `member__<f>__id` always trips C10 (its referential check).
    A records `prop__` reference has no referential C-check of its own, but
    still trips C6 when it is history_tracked and the dangled row's
    (kind, record_id, property) carries a working history series — the
    sentinel never round-trips to that series' latest value.
    """
    if is_membership_id_column(column):
        return ("C10",)
    if (
        col_spec.history_tracked
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


class DangleReferenceCorrupter:
    """Corrupter for `kind: dangle_reference` — rewrites reference ids to
    values guaranteed absent from their target records table."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, DangleReference)
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
                    if is_reference_column(col)
                ],
            )
            for population in populations
        ]
        cell_units = enumerate_cell_units(populations, per_table_columns)

        # Population filters (1)+(2) via _resolve_target_kind, and (3): the
        # target records__<kind> table must be present in the working set.
        # Cells enumerated in canonical table -> row -> column order
        # (§ Selection is faithful).
        eligible: list[tuple[int, int, str, str]] = []
        for table_idx, row_pos, column in cell_units:
            population = populations[table_idx]
            columns_by_name = {
                col.name: col for col in population.working_table.spec.columns
            }
            target_kind = _resolve_target_kind(
                column, columns_by_name[column], population.content, row_pos
            )
            if target_kind is None:
                continue
            if f"records__{target_kind}" not in state.tables:
                continue
            eligible.append((table_idx, row_pos, column, target_kind))

        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations(populations), rng
            )
            weights = unit_row_weights(eligible, row_weights)
            drawn = sorted(draw_weighted_sample(weights, operation.amount, rng))
        else:
            drawn = sorted(draw_sample(len(eligible), operation.amount, rng))
        units_selected = len(drawn)

        sentinel_by_kind = {
            kind: _smallest_absent_sentinel(_working_id_values(state, kind))
            for kind in {eligible[i][3] for i in drawn}
        }

        history_working = state.tables.get("history")
        history_data = history_working.data if history_working is not None else None

        py_columns_by_table: list[dict[str, list[object]]] = [{} for _ in populations]
        defects: list[DefectRecord] = []
        for idx in drawn:
            table_idx, row_pos, column, target_kind = eligible[idx]
            population = populations[table_idx]
            table_spec = population.working_table.spec
            columns_by_name = {col.name: col for col in table_spec.columns}
            py_columns = py_columns_by_table[table_idx]
            physical_row = population.physical_indices[row_pos]
            sentinel = sentinel_by_kind[target_kind]

            if column not in py_columns:
                py_columns[column] = population.working_table.data.column(
                    column
                ).to_pylist()
            py_columns[column][physical_row] = sentinel

            row = row_dict(population.content, row_pos)
            record_id = row["record_id"]
            assert isinstance(record_id, str)
            impact = _dangle_impact(
                column,
                columns_by_name[column],
                table_spec,
                fork_path,
                record_id,
                history_data,
            )
            row_category = row_category_for_table(table_spec)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "dangling_reference",
                        "rule": rule,
                        "impact": impact,
                        "location": cell_locator(
                            population.table_name, row_category, table_spec, row, column
                        ),
                    }
                )
            )

        write_back_pooled_columns(state, populations, py_columns_by_table)

        return OperationOutcome(
            kind="dangle_reference",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
