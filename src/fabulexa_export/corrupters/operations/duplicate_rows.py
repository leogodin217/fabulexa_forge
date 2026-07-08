"""`duplicate_rows`: exact, near-duplicate, or conflicting-duplicate row injection.

See `docs/architecture/corrupters.md` § What each operation breaks, and the
impact it declares, and § `duplicate_rows` -- the `mutation` mode, for the
impact rules and perturbation semantics this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_export.config.models import (
    DuplicateRows,
    MutationOutOfDomain,
    MutationResample,
    MutationSentinel,
    MutationTypo,
)
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    actor_subtype_undeclared,
    branch_slice_at,
    draw_delta,
    enumerate_row_units,
    history_series_exists,
    is_pinned_record_id,
    placement_populations,
    property_name_for_prop_column,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    unit_row_weights,
)
from fabulexa_export.corrupters.operations._mutations import (
    resample_donor_pool,
    sentinel_cast_cache,
    transform,
)
from fabulexa_export.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    match_column_entries,
    resolve_target_tables,
)
from fabulexa_export.corrupters.state import OperationOutcome, WorkingTable
from fabulexa_export.corrupters.validate import (
    conflict_eligible_columns,
    is_jitter_eligible,
)
from fabulexa_export.reader.conformance import _ROUND_TRIPPABLE_TYPES

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping

    from fabulexa_export.config.models import CorruptOperation, MutationSpec
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.operations._impact import TablePopulation
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar, TableSpec


def _perturb_value(value: object, delta: float, duckdb_type: str) -> object:
    """Apply one jitter delta to a non-NULL numeric payload cell.

    A DOUBLE cell stores `value + delta` as-is; a BIGINT cell stores
    `round(value + delta)` (round-half-to-even) back in the integer type, so
    the column keeps its type. A delta may vanish in the store (|delta| < 0.5
    on BIGINT, or float absorption on DOUBLE) — the caller still injects the
    copy; only the declared impact follows the actual-divergence rule.
    """
    if duckdb_type.upper() == "BIGINT":
        assert isinstance(value, int)
        return round(value + delta)
    assert isinstance(value, (int, float))
    return float(value) + delta


def _exact_duplicate_impact(
    table_spec: "TableSpec", sidecar: "Sidecar", record_id: str
) -> tuple["ImpactCode", ...]:
    """C9 iff the target is a `records__<kind>` table and `record_id` is
    pinned; beyond-c1-c12 otherwise (duplicating a membership/history row, or
    a non-pinned records row, can never trip C9)."""
    if (
        table_spec.category == "records"
        and table_spec.record_kind is not None
        and is_pinned_record_id(sidecar, table_spec.record_kind, record_id)
    ):
        return ("C9",)
    return ("beyond-c1-c12",)


def _near_duplicate_impact(
    table_spec: "TableSpec",
    sidecar: "Sidecar",
    fork_path: str,
    record_id: str,
    history_data: pa.Table | None,
    perturbed: "Mapping[str, tuple[object, object]]",
) -> tuple["ImpactCode", ...]:
    """Recompute the firing set for a near-duplicate (never inherits the
    exact case's codes): C9 iff pinned (the same rule as exact), unioned with
    C6 iff a perturbed records `prop__` has a history series for its property
    and the perturbation actually changed the stored value (actual-divergence
    rule — a vanished delta never declares C6). beyond-c1-c12 iff that union
    is empty.
    """
    codes: set["ImpactCode"] = set()
    if (
        table_spec.category == "records"
        and table_spec.record_kind is not None
        and is_pinned_record_id(sidecar, table_spec.record_kind, record_id)
    ):
        codes.add("C9")
    if table_spec.record_kind is not None:
        for column, (original, stored) in perturbed.items():
            if not column.startswith("prop__") or original == stored:
                continue
            if history_series_exists(
                history_data,
                fork_path,
                table_spec.record_kind,
                record_id,
                property_name_for_prop_column(column),
            ):
                codes.add("C6")
    if not codes:
        return ("beyond-c1-c12",)
    return tuple(sorted(codes))


def _conflict_c6_eligible(col_spec: "ColumnSpec") -> bool:
    """Whether `col_spec` is `history_tracked` with a round-trippable current
    type -- the C6 oracle's own gates, stated explicitly here because
    `mutation`'s any-type kinds (unlike numeric `jitter`) can target a
    non-round-trippable tracked column."""
    return bool(col_spec.history_tracked) and (
        col_spec.type.upper().strip() in _ROUND_TRIPPABLE_TYPES
    )


def _conflict_duplicate_impact(
    table_spec: "TableSpec",
    sidecar: "Sidecar",
    fork_path: str,
    slice_at: int,
    record_id: str,
    history_data: pa.Table | None,
    mutated: "Mapping[str, tuple[object, object]]",
) -> tuple["ImpactCode", ...]:
    """Recompute the firing set for a conflicting duplicate (never inherits
    the exact/near cases' codes): C9 iff pinned (the shipped exact/near
    rule), unioned with C6 iff a mutated records `prop__` is
    `_conflict_c6_eligible` and the copy's record has a non-empty C6 view
    (`resolve_c6_anchor` non-None) for that series and the mutation actually
    changed the stored value, unioned with C12 iff a mutated
    `prop__actor_type` rewrites the discriminator out of its declared
    sub-types (`actor_subtype_undeclared`). beyond-c1-c12 iff that union is
    empty.
    """
    codes: set["ImpactCode"] = set()
    if (
        table_spec.category == "records"
        and table_spec.record_kind is not None
        and is_pinned_record_id(sidecar, table_spec.record_kind, record_id)
    ):
        codes.add("C9")
    kind = table_spec.record_kind
    columns_by_name = {col.name: col for col in table_spec.columns}
    for column, (original, stored) in mutated.items():
        if not column.startswith("prop__") or original == stored:
            continue
        if (
            kind is not None
            and history_data is not None
            and _conflict_c6_eligible(columns_by_name[column])
            and resolve_c6_anchor(
                history_data,
                fork_path,
                slice_at,
                kind,
                property_name_for_prop_column(column),
                record_id,
            )
            is not None
        ):
            codes.add("C6")
        if isinstance(stored, str) and actor_subtype_undeclared(
            sidecar, table_spec.name, column, stored
        ):
            codes.add("C12")
    if not codes:
        return ("beyond-c1-c12",)
    return tuple(sorted(codes))


def _duplicate_domain_for_column(
    column: str, table_spec: "TableSpec", sidecar: "Sidecar"
) -> "frozenset[str]":
    """`out_of_domain`'s declared domain values for one conflict-eligible
    `prop__<p>` column. Presence is guaranteed by `conflict_eligible_columns`'
    enum-domain gate."""
    kind = table_spec.record_kind
    assert kind is not None
    property_name = property_name_for_prop_column(column)
    return frozenset(sidecar.enum_domains()[kind][property_name])


def _duplicate_seed_inputs(
    mutation: "MutationSpec",
    population: "TablePopulation",
    column: str,
    table_idx: int,
    fork_path: str,
    sidecar: "Sidecar",
    domain_cache: dict[tuple[int, str], "frozenset[str]"],
    donor_cache: dict[tuple[int, str], list[object]],
) -> tuple["frozenset[str] | None", list[object] | None]:
    """The per-column `(domain, donor_pool)` inputs `transform` needs for the
    seeded kinds, computed once per (table, column) and cached. Never
    narrowed to a history series -- `conflict_eligible_columns` excludes
    fixed-category tables, so `duplicate_rows.mutation` never targets
    `history.value`.

    Args:
        mutation: The operation's mutation spec.
        population: The row's resolved table population.
        column: The matched column.
        table_idx: The row's resolved-table index (the cache key's table half).
        fork_path: The sole branch's fork_path.
        sidecar: The source emit's sidecar.
        domain_cache: `out_of_domain`'s domain cache, mutated in place.
        donor_cache: `resample`'s donor-pool cache, mutated in place.

    Returns:
        `(domain, None)` for `out_of_domain`, `(None, donor_pool)` for
        `resample`, `(None, None)` for every other kind.
    """
    key = (table_idx, column)
    if isinstance(mutation, MutationOutOfDomain):
        if key not in domain_cache:
            domain_cache[key] = _duplicate_domain_for_column(
                column, population.working_table.spec, sidecar
            )
        return domain_cache[key], None
    if isinstance(mutation, MutationResample):
        if key not in donor_cache:
            donor_cache[key] = resample_donor_pool(
                population.working_table, fork_path, column, None
            )
        return None, donor_cache[key]
    return None, None


class DuplicateRowsCorrupter:
    """Corrupter for `kind: duplicate_rows` — exact, near-duplicate, or
    conflicting-duplicate injection."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, DuplicateRows)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        jitter = operation.jitter
        mutation = operation.mutation
        if jitter is not None:
            entries = operation.target.columns
            assert entries is not None
            per_table_columns = [
                match_column_entries(
                    entries,
                    [
                        col.name
                        for col in population.working_table.spec.columns
                        if is_jitter_eligible(col)
                    ],
                )
                for population in populations
            ]
            table_included = [len(columns) > 0 for columns in per_table_columns]
        elif mutation is not None:
            entries = operation.target.columns
            assert entries is not None
            per_table_columns = [
                match_column_entries(
                    entries,
                    conflict_eligible_columns(
                        mutation, population.working_table.spec, sidecar
                    ),
                )
                for population in populations
            ]
            table_included = [len(columns) > 0 for columns in per_table_columns]
        else:
            per_table_columns = [[] for _ in populations]
            table_included = [True for _ in populations]

        row_units = enumerate_row_units(populations, table_included)
        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations(populations), rng
            )
            weights = unit_row_weights(row_units, row_weights)
            drawn = draw_weighted_sample(weights, operation.amount, rng)
        else:
            drawn = draw_sample(len(row_units), operation.amount, rng)
        units_selected = len(drawn)

        sentinel_cache: dict[tuple[str, str], object] = {}
        if isinstance(mutation, MutationSentinel):
            sentinel_cache = sentinel_cast_cache(
                mutation, populations, per_table_columns, rule, "duplicate_rows"
            )
        is_seeded = isinstance(
            mutation, MutationTypo | MutationResample | MutationOutOfDomain
        )
        domain_cache: dict[tuple[int, str], "frozenset[str]"] = {}
        donor_cache: dict[tuple[int, str], list[object]] = {}

        history_working = state.tables.get("history")
        history_data = history_working.data if history_working is not None else None
        # Only the mutation-mode C6 gate needs slice_at; exact/near modes
        # never call branch_slice_at, so their tests need no branches entry.
        slice_at = branch_slice_at(sidecar, fork_path) if mutation is not None else 0
        if mutation is not None:
            defect_class = "conflicting_duplicate_row"
        elif jitter is not None:
            defect_class = "near_duplicate_row"
        else:
            defect_class = "duplicate_row"

        new_rows_by_table: list[list[dict[str, object]]] = [[] for _ in populations]
        defects: list[DefectRecord] = []
        for unit_index in sorted(drawn):
            table_idx, row_pos = row_units[unit_index]
            population = populations[table_idx]
            table_spec = population.working_table.spec
            columns_by_name = {col.name: col for col in table_spec.columns}
            original = row_dict(population.content, row_pos)
            record_id = original["record_id"]
            assert isinstance(record_id, str)
            new_row = dict(original)
            if jitter is not None:
                perturbed: dict[str, tuple[object, object]] = {}
                for column in per_table_columns[table_idx]:
                    value = original[column]
                    if value is None:
                        continue
                    delta = draw_delta(jitter, rng)
                    stored = _perturb_value(value, delta, columns_by_name[column].type)
                    new_row[column] = stored
                    perturbed[column] = (value, stored)
                impact = _near_duplicate_impact(
                    table_spec, sidecar, fork_path, record_id, history_data, perturbed
                )
            elif mutation is not None:
                mutated: dict[str, tuple[object, object]] = {}
                for column in per_table_columns[table_idx]:
                    seed = rng.random() if is_seeded else None
                    value = original[column]
                    if value is None:
                        continue
                    domain, donor_pool = _duplicate_seed_inputs(
                        mutation,
                        population,
                        column,
                        table_idx,
                        fork_path,
                        sidecar,
                        domain_cache,
                        donor_cache,
                    )
                    stored = transform(
                        mutation,
                        value,
                        population.table_name,
                        column,
                        sentinel_cache,
                        seed,
                        domain,
                        donor_pool,
                    )
                    if stored == value:
                        continue
                    new_row[column] = stored
                    mutated[column] = (value, stored)
                impact = _conflict_duplicate_impact(
                    table_spec,
                    sidecar,
                    fork_path,
                    slice_at,
                    record_id,
                    history_data,
                    mutated,
                )
            else:
                impact = _exact_duplicate_impact(table_spec, sidecar, record_id)
            new_rows_by_table[table_idx].append(new_row)
            row_category = row_category_for_table(table_spec)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": defect_class,
                        "rule": rule,
                        "impact": impact,
                        "location": row_locator(
                            population.table_name, row_category, table_spec, new_row
                        ),
                    }
                )
            )

        for table_idx, population in enumerate(populations):
            new_rows = new_rows_by_table[table_idx]
            if not new_rows:
                continue
            schema = population.working_table.data.schema
            new_rows_table = pa.Table.from_pylist(new_rows, schema=schema)
            new_data = pa.concat_tables([population.working_table.data, new_rows_table])
            state.tables[population.table_name] = WorkingTable(
                spec=population.working_table.spec, data=new_data
            )

        return OperationOutcome(
            kind="duplicate_rows",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
