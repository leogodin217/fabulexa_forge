"""`insert_rows`: phantom-row injection into records-category tables.

See `docs/architecture/corrupters.md` § `insert_rows` for the population/unit,
id-universe, phantom-assembly, and impact rule this handler implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from fabulexa_forge.config.models import InsertRows
from fabulexa_forge.corrupters.manifest import DefectRecord
from fabulexa_forge.corrupters.operations._impact import (
    enumerate_row_units,
    kind_has_tracked_genesis_property,
    membership_kind_id_pairs,
    placement_populations,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    row_locator,
    unit_row_weights,
    with_c13,
)
from fabulexa_forge.corrupters.operations._mutations import (
    apply_resample,
    resample_donor_pool,
    rotation,
    swap_adjacent,
)
from fabulexa_forge.corrupters.selection import (
    derive_row_weights,
    draw_sample,
    draw_weighted_sample,
    match_column_entries,
    resolve_target_tables,
)
from fabulexa_forge.corrupters.state import OperationOutcome, WorkingTable
from fabulexa_forge.corrupters.validate import insert_eligible_columns

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

    from fabulexa_forge.config.models import CorruptOperation
    from fabulexa_forge.corrupters.state import CorruptState
    from fabulexa_forge.reader.sidecar import Sidecar


def _kind_id_universe(state: "CorruptState", sidecar: "Sidecar", kind: str) -> set[str]:
    """The absence domain for a fresh kind-K phantom id, at the operation's
    start (design doc § Semantics, `insert_rows` -- the id universe): the
    working `records__<K>` ids, `history.record_id` values for K, non-NULL
    `member__<f>__id` cells whose partner kind cell is K, non-NULL cells of
    every records `prop__` column whose `references` target is K, the
    sidecar's pinned ids for K, and K's working tombstones.

    Args:
        state: The working set, as of this operation's start.
        sidecar: The source emit's sidecar.
        kind: The donor's own records kind (K).

    Returns:
        The id universe, mutated in place by the caller as phantoms are
        assigned (buying "earlier phantoms of the same operation" for free).
    """
    universe: set[str] = set()

    records_table = state.tables.get(f"records__{kind}")
    if records_table is not None:
        universe.update(records_table.data.column("record_id").to_pylist())

    history_table = state.tables.get("history")
    if history_table is not None:
        data = history_table.data
        kinds = data.column("kind")
        record_ids = data.column("record_id")
        for i in range(data.num_rows):
            if kinds[i].as_py() == kind:
                universe.add(record_ids[i].as_py())

    for kind_val, id_val in membership_kind_id_pairs(state):
        if kind_val == kind:
            universe.add(id_val)

    for working_table in state.tables.values():
        spec = working_table.spec
        if spec.category != "records":
            continue
        data = working_table.data
        for col in spec.columns:
            if col.name.startswith("prop__") and col.references == kind:
                for value in data.column(col.name).to_pylist():
                    if value is not None:
                        universe.add(value)

    universe.update(sidecar.pinned_ids().get(kind, {}).values())
    universe.update(state.deleted_record_ids.get(kind, set()))
    return universe


def _derive_phantom_id(donor_id: str, seed: float, universe: set[str]) -> str:
    """A fresh id derived from `donor_id`, absent from `universe` (design doc
    § Semantics, `insert_rows` -- phantom assembly): the first adjacent-
    character exchange, positions scanned in seeded rotation, absent from
    `universe`; failing that, the total fallback of `donor_id` with its final
    character (or `"0"`, when `donor_id` is empty) appended repeatedly.

    Args:
        donor_id: The donor row's record_id.
        seed: The id-derivation rotation's per-phantom draw.
        universe: The kind's id universe (donor_id is always a member, so a
            transposition that reproduces it is never chosen).

    Returns:
        A fresh id, guaranteed absent from `universe`.
    """
    for pos in rotation(len(donor_id) - 1, seed):
        candidate = swap_adjacent(donor_id, pos)
        if candidate not in universe:
            return candidate
    filler = donor_id[-1] if donor_id else "0"
    candidate = donor_id
    while True:
        candidate += filler
        if candidate not in universe:
            return candidate


class InsertRowsCorrupter:
    """Corrupter for `kind: insert_rows` -- clones sampled donor rows under
    fresh, plausible record_ids; optionally resamples matched payload cells."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        assert isinstance(operation, InsertRows)
        table_names = resolve_target_tables(operation.target, sidecar)
        populations = resolve_pooled_populations(
            state, table_names, fork_path, operation.target.where
        )
        entries = operation.target.columns
        if entries is not None:
            per_table_columns: "Sequence[Sequence[str]]" = [
                match_column_entries(
                    entries, insert_eligible_columns(population.working_table.spec)
                )
                for population in populations
            ]
        else:
            per_table_columns = [[] for _ in populations]

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
                kind="insert_rows",
                tables=tuple(table_names),
                units_selected=0,
                units_affected=0,
                defects=(),
            )

        id_universe_cache: dict[str, set[str]] = {}
        c13_by_kind: dict[str, bool] = {}
        donor_pool_cache: dict[tuple[int, str], list[object]] = {}
        new_rows_by_table: list[list[dict[str, object]]] = [[] for _ in populations]
        defects: list[DefectRecord] = []
        for unit_index in sorted(drawn):
            table_idx, row_pos = row_units[unit_index]
            population = populations[table_idx]
            table_spec = population.working_table.spec
            kind = table_spec.record_kind
            assert kind is not None  # RecordsCategoryTarget confines to records

            if kind not in id_universe_cache:
                id_universe_cache[kind] = _kind_id_universe(state, sidecar, kind)
            universe = id_universe_cache[kind]

            donor = row_dict(population.content, row_pos)
            donor_id = donor["record_id"]
            assert isinstance(donor_id, str)
            # RNG order: one draw for the id-derivation rotation, then one
            # draw per resolved resample column, per phantom.
            id_seed = rng.random()
            phantom_id = _derive_phantom_id(donor_id, id_seed, universe)
            universe.add(phantom_id)

            phantom = dict(donor)
            phantom["record_id"] = phantom_id
            phantom["record_index"] = state.mint_record_index(population.table_name)
            for column in per_table_columns[table_idx]:
                seed = rng.random()
                current = donor[column]
                if current is None:
                    continue  # NULL-invariance: a NULL cloned cell stays NULL
                cache_key = (table_idx, column)
                if cache_key not in donor_pool_cache:
                    donor_pool_cache[cache_key] = resample_donor_pool(
                        population.working_table, fork_path, column, None
                    )
                phantom[column] = apply_resample(
                    current, seed, donor_pool_cache[cache_key]
                )

            new_rows_by_table[table_idx].append(phantom)
            # C13: a phantom carries no history, so it lacks its genesis row for
            # every history_tracked, round-trippable prop of its kind. When the
            # kind has no such property the phantom breaks no conformance code.
            if kind not in c13_by_kind:
                c13_by_kind[kind] = kind_has_tracked_genesis_property(
                    population.working_table
                )
            impact = with_c13(("beyond-c1-c12",), c13_by_kind[kind])
            row_category = row_category_for_table(table_spec)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": "phantom_row",
                        "rule": rule,
                        "impact": impact,
                        "location": row_locator(
                            population.table_name, row_category, table_spec, phantom
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
            kind="insert_rows",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
