"""`mutate_cells`: wrong-value injection over a sampled set of value cells.

See `docs/architecture/pending/corrupter-cell-value-mutations.md` § What each
mutation does, § The impact rule (normative) for the per-kind transform table
and the anchor-participation / round-trip / sub-type impact rule this handler
implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from fabulexa_export.config.models import (
    MutateCells,
    MutationOutOfDomain,
    MutationResample,
    MutationSentinel,
)
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    actor_subtype_undeclared,
    branch_slice_at,
    cell_locator,
    current_value,
    enumerate_cell_units,
    placement_populations,
    property_name_for_prop_column,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    series_round_trip_fails,
    unit_row_weights,
    write_back_pooled_columns,
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
from fabulexa_export.corrupters.state import OperationOutcome
from fabulexa_export.corrupters.validate import mutation_eligible_columns

if TYPE_CHECKING:
    import random

    import pyarrow as pa

    from fabulexa_export.config.models import CorruptOperation, MutationSpec
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.operations._impact import TablePopulation
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import Sidecar, TableSpec


_DEFECT_CLASS: dict[str, str] = {
    "sentinel": "sentinel_value",
    "typo": "typo_value",
    "case": "case_drift",
    "whitespace": "whitespace_pad",
    "truncate": "truncated_value",
    "precision_drop": "precision_drop",
    "scale": "scaled_value",
    "mojibake": "mojibake_value",
    "format_dirt": "format_dirt",
    "resample": "resampled_value",
    "out_of_domain": "out_of_domain_value",
}
"""One `defect_class` per mutation kind (§ Family-wide rules)."""

_SEEDED_MUTATION_KINDS: frozenset[str] = frozenset(
    {"typo", "resample", "out_of_domain"}
)
"""RNG slot-(3): the mutation kinds that draw one `rng.random()` per selected
unit (edit position / donor index / candidate rotation). The other eight
kinds are deterministic transforms and draw nothing."""


class _Mutation(NamedTuple):
    """One performed cell rewrite: which pooled unit, its pre-mutation row
    content (for the locator/record_id), and the value it was rewritten to."""

    table_idx: int
    column: str
    pre_row: dict[str, object]
    post_value: object


def _history_value_series(
    population: "TablePopulation", column: str, row_pos: int
) -> tuple[str, str] | None:
    """`resample`'s `(kind, property)` narrowing key for a `history.value`
    cell -- the pool is the same series' value population, never mixed
    across properties.

    Args:
        population: The cell's resolved table population.
        column: The cell's resolved column.
        row_pos: The cell's canonical-order row position in
            `population.content`.

    Returns:
        `(kind, property)`, or None when `column` is not `history.value`.
    """
    if column != "value" or population.working_table.spec.category != "fixed":
        return None
    kind = population.content.column("kind")[row_pos].as_py()
    property_name = population.content.column("property")[row_pos].as_py()
    assert isinstance(kind, str)
    assert isinstance(property_name, str)
    return kind, property_name


def _domain_for_column(
    column: str, table_spec: "TableSpec", sidecar: "Sidecar"
) -> "frozenset[str]":
    """`out_of_domain`'s declared domain values for one `prop__<p>` column.
    Presence is guaranteed by `mutation_eligible_columns`' enum-domain gate."""
    kind = table_spec.record_kind
    assert kind is not None
    property_name = property_name_for_prop_column(column)
    return frozenset(sidecar.enum_domains()[kind][property_name])


def _seed_inputs(
    mutation: "MutationSpec",
    population: "TablePopulation",
    column: str,
    table_idx: int,
    row_pos: int,
    fork_path: str,
    sidecar: "Sidecar",
    domain_cache: dict[tuple[int, str], "frozenset[str]"],
    donor_cache: dict[tuple[int, str, str, str], list[object]],
) -> tuple["frozenset[str] | None", list[object] | None]:
    """The per-unit `(domain, donor_pool)` inputs `transform` needs for the
    seeded kinds, computed once per (table, column[, series]) key and cached
    in `domain_cache` / `donor_cache`.

    Args:
        mutation: The operation's mutation spec.
        population: The cell's resolved table population.
        column: The cell's resolved column.
        table_idx: The cell's resolved-table index (the cache key's table half).
        row_pos: The cell's canonical-order row position, for `history.value`'s
            per-series donor-pool narrowing.
        fork_path: The sole branch's fork_path.
        sidecar: The source emit's sidecar.
        domain_cache: `out_of_domain`'s domain cache, mutated in place.
        donor_cache: `resample`'s donor-pool cache, keyed by
            `(table_idx, column, kind, property)` -- `kind`/`property` are
            empty strings for every column but `history.value`; mutated in
            place.

    Returns:
        `(domain, None)` for `out_of_domain`, `(None, donor_pool)` for
        `resample`, `(None, None)` for every other kind.
    """
    key_domain = (table_idx, column)
    if isinstance(mutation, MutationOutOfDomain):
        if key_domain not in domain_cache:
            domain_cache[key_domain] = _domain_for_column(
                column, population.working_table.spec, sidecar
            )
        return domain_cache[key_domain], None
    if isinstance(mutation, MutationResample):
        series = _history_value_series(population, column, row_pos)
        kind, property_name = series if series is not None else ("", "")
        key_donor = (table_idx, column, kind, property_name)
        if key_donor not in donor_cache:
            donor_cache[key_donor] = resample_donor_pool(
                population.working_table, fork_path, column, series
            )
        return None, donor_cache[key_donor]
    return None, None


def _cell_impact(
    column: str,
    table_name: str,
    table_spec: "TableSpec",
    record_id: str,
    post_value: object,
    state: "CorruptState",
    sidecar: "Sidecar",
    fork_path: str,
    slice_at: int,
) -> tuple["ImpactCode", ...]:
    """The impact rule for one mutated records/membership cell, against the
    post-operation working state (§ The impact rule). Never called for
    `history.value`; see `_history_value_impact`.

    records `prop__<p>`: `C6` iff the series' post-operation round-trip
    fails; `C12` iff `actor_subtype_undeclared`; union when both hold.
    `presentation_id` / `elem__*`: never.
    """
    if not column.startswith("prop__"):
        return ("beyond-c1-c12",)
    kind = table_spec.record_kind
    assert kind is not None  # MutableColumns confines prop__ to records tables
    codes: list["ImpactCode"] = []
    if series_round_trip_fails(
        state,
        fork_path,
        slice_at,
        kind,
        property_name_for_prop_column(column),
        record_id,
    ):
        codes.append("C6")
    if isinstance(post_value, str) and actor_subtype_undeclared(
        sidecar, table_name, column, post_value
    ):
        codes.append("C12")
    return tuple(codes) if codes else ("beyond-c1-c12",)


def _history_value_impact(
    pre_row: dict[str, object],
    pre_history: "pa.Table",
    post_value: object,
    state: "CorruptState",
    fork_path: str,
    slice_at: int,
) -> tuple["ImpactCode", ...]:
    """The impact rule for one mutated `history.value` cell (§ The impact rule).

    `C6` iff the mutated row held the series' anchor in the
    operation-start state (`resolve_c6_anchor` against `pre_history`), or its
    post-mutation `(sim_time, value)` pair equals the post-operation anchor
    -- and the post-operation round-trip fails for that series;
    `beyond-c1-c12` otherwise. A post-`slice_at` row can never satisfy either
    participation clause (`resolve_c6_anchor` excludes rows past `slice_at`),
    so it always declares `beyond-c1-c12`. `history.value` never declares C12.

    Args:
        pre_row: The mutated row's pre-mutation content (at minimum kind,
            property, record_id, sim_time, value).
        pre_history: The working `history` table as of this operation's
            start, for the pre-mutation anchor resolution.
        post_value: The cell's post-mutation stored value.
        state: The shared working set, as of after this operation's writes.
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at.

    Returns:
        `("C6",)` or `("beyond-c1-c12",)`.

    Raises:
        CorruptError: The working `history` table is absent from `state`.
    """
    kind = pre_row["kind"]
    property_name = pre_row["property"]
    record_id = pre_row["record_id"]
    sim_time = pre_row["sim_time"]
    pre_value = pre_row["value"]
    assert isinstance(kind, str)
    assert isinstance(property_name, str)
    assert isinstance(record_id, str)
    assert isinstance(sim_time, int)
    assert isinstance(post_value, str)

    if not series_round_trip_fails(
        state, fork_path, slice_at, kind, property_name, record_id
    ):
        return ("beyond-c1-c12",)

    pre_anchor = resolve_c6_anchor(
        pre_history, fork_path, slice_at, kind, property_name, record_id
    )
    history_working = state.tables["history"]
    post_anchor = resolve_c6_anchor(
        history_working.data, fork_path, slice_at, kind, property_name, record_id
    )
    was_anchor = pre_anchor == (sim_time, pre_value)
    becomes_anchor = post_anchor == (sim_time, post_value)
    if was_anchor or becomes_anchor:
        return ("C6",)
    return ("beyond-c1-c12",)


class MutateCellsCorrupter:
    """Corrupter for `kind: mutate_cells` -- rewrites a sampled set of value
    cells with one type-preserving mutation."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, MutateCells)
        entries = operation.target.columns
        assert entries is not None
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        per_table_columns = [
            match_column_entries(
                entries,
                mutation_eligible_columns(
                    operation.mutation, population.working_table.spec, sidecar
                ),
            )
            for population in populations
        ]

        sentinel_cache: dict[tuple[str, str], object] = {}
        if isinstance(operation.mutation, MutationSentinel):
            sentinel_cache = sentinel_cast_cache(
                operation.mutation, populations, per_table_columns, rule, "mutate_cells"
            )

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

        is_seeded = operation.mutation.kind in _SEEDED_MUTATION_KINDS
        domain_cache: dict[tuple[int, str], "frozenset[str]"] = {}
        donor_cache: dict[tuple[int, str, str, str], list[object]] = {}
        py_columns_by_table: list[dict[str, list[object]]] = [{} for _ in populations]
        mutations: list[_Mutation] = []
        for unit_index in sorted(drawn):
            table_idx, row_pos, column = pooled_units[unit_index]
            population = populations[table_idx]
            # Slot (3): one mode draw per selected unit for the seeded kinds,
            # consumed before the no-mutation gate (a fixed function of the
            # selected-unit count).
            seed = rng.random() if is_seeded else None
            content = population.content
            py_columns = py_columns_by_table[table_idx]
            physical_row = population.physical_indices[row_pos]
            current = current_value(py_columns, content, row_pos, physical_row, column)
            if current is None:
                continue  # NULL-invariance: never mutated

            domain, donor_pool = _seed_inputs(
                operation.mutation,
                population,
                column,
                table_idx,
                row_pos,
                fork_path,
                sidecar,
                domain_cache,
                donor_cache,
            )
            post_value = transform(
                operation.mutation,
                current,
                population.table_name,
                column,
                sentinel_cache,
                seed,
                domain,
                donor_pool,
            )
            if post_value == current:
                continue  # no-mutation unit: not counted, no defect

            pre_row = {
                name: current_value(py_columns, content, row_pos, physical_row, name)
                for name in content.schema.names
            }
            mutations.append(_Mutation(table_idx, column, pre_row, post_value))
            if column not in py_columns:
                py_columns[column] = population.working_table.data.column(
                    column
                ).to_pylist()
            py_columns[column][physical_row] = post_value

        write_back_pooled_columns(state, populations, py_columns_by_table)

        slice_at = branch_slice_at(sidecar, fork_path)
        defect_class = _DEFECT_CLASS[operation.mutation.kind]
        defects: list[DefectRecord] = []
        for mutation in mutations:
            population = populations[mutation.table_idx]
            table_spec = population.working_table.spec
            record_id = mutation.pre_row["record_id"]
            assert isinstance(record_id, str)
            if table_spec.category == "fixed":
                impact = _history_value_impact(
                    mutation.pre_row,
                    population.working_table.data,
                    mutation.post_value,
                    state,
                    fork_path,
                    slice_at,
                )
            else:
                impact = _cell_impact(
                    mutation.column,
                    population.table_name,
                    table_spec,
                    record_id,
                    mutation.post_value,
                    state,
                    sidecar,
                    fork_path,
                    slice_at,
                )
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": defect_class,
                        "rule": rule,
                        "impact": impact,
                        "location": cell_locator(
                            population.table_name,
                            row_category_for_table(table_spec),
                            table_spec,
                            mutation.pre_row,
                            mutation.column,
                        ),
                    }
                )
            )

        return OperationOutcome(
            kind="mutate_cells",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
