"""`mispoint_reference`: referential mis-pointing over a sampled set of
reference cells -- rewrite each selected cell's id to a wrong-but-real donor
drawn from the same target table, so the cell keeps resolving (green RI) while
pointing at the wrong row.

See `docs/architecture/pending/corrupter-mispoint-reference.md` § The
population: four filters, § The donor pool, § The draw and the rewrite, § The
impact rule (normative) for the population filters, donor-pool resolution,
RNG discipline, and impact rule this handler implements. Structurally a
`dangle_reference` sibling (filters 1-3 verbatim); the membership-id-column
test the two share lives in `_impact.py` as `is_membership_id_column`.
`_resolve_target_kind_and_id` below is this module's own -- it pairs the
target kind with the cell's current id, which `dangle_reference`'s
`_resolve_target_kind` does not need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from fabulexa_export.config.models import MispointReference
from fabulexa_export.corrupters.manifest import DefectRecord
from fabulexa_export.corrupters.operations._impact import (
    branch_slice_at,
    cell_locator,
    enumerate_cell_units,
    is_membership_id_column,
    membership_partner_column,
    placement_populations,
    property_name_for_prop_column,
    resolve_c6_anchor,
    resolve_pooled_populations,
    row_category_for_table,
    row_dict,
    series_round_trip_fails,
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
from fabulexa_export.errors import CorruptError

if TYPE_CHECKING:
    import random

    import pyarrow as pa

    from fabulexa_export.config.models import CorruptOperation
    from fabulexa_export.corrupters.manifest import ImpactCode
    from fabulexa_export.corrupters.state import CorruptState
    from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar, TableSpec


def _resolve_target_kind_and_id(
    column: str, col_spec: "ColumnSpec", content: "pa.Table", row_pos: int
) -> tuple[str, str] | None:
    """Population filters (1)+(2), paired with the cell's current stored id.

    Args:
        column: The reference column's current name.
        col_spec: The column's current ColumnSpec.
        content: The canonically-ordered population content.
        row_pos: The row's 0-based canonical-order position.

    Returns:
        `(target_kind, current_id)`, or None when population-filtered: the
        id itself is NULL (filter 1), or -- for a membership id column --
        its partner `member__<f>__kind` is also NULL (filter 2).
    """
    id_value = content.column(column)[row_pos].as_py()
    if id_value is None:
        return None
    assert isinstance(id_value, str)
    if is_membership_id_column(column):
        partner = membership_partner_column(column)
        kind_value = content.column(partner)[row_pos].as_py()
        if kind_value is None:
            return None
        assert isinstance(kind_value, str)
        return kind_value, id_value
    assert col_spec.references is not None
    return col_spec.references, id_value


def resolve_donor_pool(
    state: "CorruptState",
    fork_path: str,
    target_kind: str,
    current_id: str,
    minimum_created_sim_time: int | None,
) -> tuple[str, ...]:
    """The sorted donor pool for one reference cell.

    Distinct `record_id` values of the working `records__<target_kind>` table
    on `fork_path`, excluding `current_id`, optionally restricted to donors
    whose creation time (minimum `created_sim_time` among the donor's rows on
    `fork_path`) is strictly greater than `minimum_created_sim_time`.
    Lexicographically ascending -- the seeded donor draw indexes this order.

    Args:
        state: The shared working set, as of the operation's start.
        fork_path: The sole branch's fork_path; the donor universe and
            creation times read only rows carrying it. (Single-branch stage:
            every working row does, so the narrowing is definitionally a
            no-op -- stated so the contract says what the values mean.)
        target_kind: The cell's resolved target records kind.
        current_id: The cell's current stored id (excluded from the pool).
        minimum_created_sim_time: None for the unconstrained mode; otherwise
            the cell's write anchor -- an exclusive lower bound on donor
            creation time.

    Returns:
        The sorted donor ids; empty when no donor qualifies (the cell is
        then population-filtered, never an error).

    Raises:
        CorruptError: `records__<target_kind>` is absent from the working
            set -- an engine-invariant breach (filter 3 excludes such cells
            before this resolver runs), not a config error.
    """
    table_name = f"records__{target_kind}"
    working = state.tables.get(table_name)
    if working is None:
        raise CorruptError(
            f"resolve_donor_pool: working set carries no {table_name!r} table"
        )
    data = working.data
    fork_paths = data.column("fork_path")
    record_ids = data.column("record_id")
    if minimum_created_sim_time is None:
        donors = {
            record_id
            for i in range(data.num_rows)
            if fork_paths[i].as_py() == fork_path
            and (record_id := record_ids[i].as_py()) != current_id
        }
        return tuple(sorted(donors))

    created_sim_times = data.column("created_sim_time")
    creation_times: dict[str, int] = {}
    for i in range(data.num_rows):
        if fork_paths[i].as_py() != fork_path:
            continue
        record_id = record_ids[i].as_py()
        if record_id == current_id:
            continue
        created = created_sim_times[i].as_py()
        earliest = creation_times.get(record_id)
        if earliest is None or created < earliest:
            creation_times[record_id] = created
    donors = {
        record_id
        for record_id, created in creation_times.items()
        if created > minimum_created_sim_time
    }
    return tuple(sorted(donors))


def _row_sim_time(row: dict[str, object], key: str) -> int:
    """A contract-pinned lifecycle `sim_time` column's value from `row`.

    Args:
        row: The referencing row (canonical population content).
        key: The lifecycle column's name (`joined_sim_time` or
            `last_mutation_sim_time`).

    Returns:
        The column's value.

    Raises:
        CorruptError: `row` carries no `key` column -- an engine-invariant
            breach (the base contract pins this column on every row of its
            category), not a config error.
    """
    if key not in row:
        raise CorruptError(
            f"resolve_reference_write_anchor: row missing contract-pinned"
            f" column {key!r}"
        )
    value = row[key]
    assert isinstance(value, int)
    return value


def resolve_reference_write_anchor(
    state: "CorruptState",
    fork_path: str,
    slice_at: int,
    table_spec: "TableSpec",
    column_spec: "ColumnSpec",
    row: dict[str, object],
) -> int:
    """An upper bound on the sim_time at which this reference cell was written.

    Membership id column: the row's `joined_sim_time`. Records `prop__`
    reference on a `record_kind`-bearing table, `history_tracked`, with a
    resolvable C6 anchor in the working history (`resolve_c6_anchor`
    non-None): that anchor's sim_time -- the exact write time of the current
    value. Any other records reference (untracked, no `record_kind`, no
    series, or empty C6 view): the row's `last_mutation_sim_time` -- no
    property write postdates it, so the point-in-time label stays sound.

    Args:
        state: The shared working set, as of the operation's start.
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at (sidecar-sourced).
        table_spec: The referencing table's current working spec.
        column_spec: The reference column's current ColumnSpec.
        row: The referencing row (canonical population content).

    Returns:
        The write anchor in sim_time ns.

    Raises:
        CorruptError: The tracked-reference path -- the only path that reads
            `history` -- finds the working history table absent from the
            state, or a contract-pinned lifecycle column is missing from
            `row` -- engine-invariant breaches, not config errors. The
            membership and `last_mutation_sim_time` fallback paths never
            read `history` and cannot raise the first.
    """
    if is_membership_id_column(column_spec.name):
        return _row_sim_time(row, "joined_sim_time")

    if table_spec.record_kind is not None and column_spec.history_tracked:
        history_working = state.tables.get("history")
        if history_working is None:
            raise CorruptError(
                "resolve_reference_write_anchor: working set carries no 'history' table"
            )
        record_id = row["record_id"]
        assert isinstance(record_id, str)
        anchor = resolve_c6_anchor(
            history_working.data,
            fork_path,
            slice_at,
            table_spec.record_kind,
            property_name_for_prop_column(column_spec.name),
            record_id,
        )
        if anchor is not None:
            return anchor[0]

    return _row_sim_time(row, "last_mutation_sim_time")


def mispoint_impact(
    state: "CorruptState",
    column: str,
    col_spec: "ColumnSpec",
    table_spec: "TableSpec",
    fork_path: str,
    slice_at: int,
    record_id: str,
) -> tuple["ImpactCode", ...]:
    """The impact rule for one mis-pointed reference cell.

    A membership `member__<f>__id` declares ("beyond-c1-c12",) -- the donor
    resolves by construction. A records `prop__` reference declares ("C6",)
    iff the column is history_tracked, the table has a record_kind, and
    `series_round_trip_fails` holds on the post-write state (the
    actual-divergence stance); ("beyond-c1-c12",) otherwise.

    Args:
        state: The shared working set, after this operation's write-back.
        column: The reference column's current name.
        col_spec: The column's current ColumnSpec.
        table_spec: The referencing table's current working spec.
        fork_path: The sole branch's fork_path.
        slice_at: The sole branch's slice_at (sidecar-sourced).
        record_id: The referencing row's record_id.

    Returns:
        The normalized impact tuple -- ("C6",) or ("beyond-c1-c12",).

    Raises:
        CorruptError: The records tracked path consults
            `series_round_trip_fails` and the working history table is
            absent from the state -- an engine-invariant breach, not a
            config error. The membership path never reads `history` and
            cannot raise.
    """
    if is_membership_id_column(column):
        return ("beyond-c1-c12",)
    if (
        col_spec.history_tracked
        and table_spec.record_kind is not None
        and series_round_trip_fails(
            state,
            fork_path,
            slice_at,
            table_spec.record_kind,
            property_name_for_prop_column(column),
            record_id,
        )
    ):
        return ("C6",)
    return ("beyond-c1-c12",)


class _Mispoint(NamedTuple):
    """One performed cell rewrite: which resolved table, its column, its
    pre-write row content (for the locator/record_id), and its drawn donor."""

    table_idx: int
    column: str
    row: dict[str, object]
    donor: str


class MispointReferenceCorrupter:
    """Corrupter for `kind: mispoint_reference` -- rewrites reference ids to
    wrong-but-real target ids drawn from a per-cell donor pool."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> OperationOutcome:
        """Apply one mispoint_reference operation to the working set.

        Resolves the pooled reference-cell population (four filters), draws
        units (uniform or placed), draws one donor per selected unit, writes
        the donors back, and declares one DefectRecord per unit with the
        post-write impact rule.

        Args:
            state: The shared working set; mutated in place.
            operation: The validated MispointReference model.
            rule: The defect `rule` label ("name" or "mispoint_reference#<i>").
            rng: The operation's seeded RNG sub-stream.
            fork_path: The sole branch's fork_path.
            sidecar: The source sidecar (selector resolution; slice_at).

        Returns:
            OperationOutcome with kind "mispoint_reference",
            units_affected == units_selected == len(defects).

        Raises:
            CorruptError: An engine-invariant breach (absent working
                history, missing contract-pinned columns) -- never a
                data-dependent empty population, which is a no-op.
        """
        assert isinstance(operation, MispointReference)
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
        slice_at = branch_slice_at(sidecar, fork_path)
        constrained = operation.constraint is not None

        # Population filters (1)+(2) via _resolve_target_kind_and_id, (3):
        # the target records__<kind> table must be present in the working
        # set, (4, new): the donor pool must be non-empty. Cells enumerated
        # in canonical table -> row -> column order (§ Selection is faithful).
        # eligible_units/eligible_pools are parallel lists, so unit_row_weights
        # sees the shipped (table_index, row_pos, column) cell-unit shape.
        eligible_units: list[tuple[int, int, str]] = []
        eligible_pools: list[tuple[str, ...]] = []
        for table_idx, row_pos, column in cell_units:
            population = populations[table_idx]
            columns_by_name = {
                col.name: col for col in population.working_table.spec.columns
            }
            resolved = _resolve_target_kind_and_id(
                column, columns_by_name[column], population.content, row_pos
            )
            if resolved is None:
                continue
            target_kind, current_id = resolved
            if f"records__{target_kind}" not in state.tables:
                continue
            minimum_created_sim_time: int | None = None
            if constrained:
                minimum_created_sim_time = resolve_reference_write_anchor(
                    state,
                    fork_path,
                    slice_at,
                    population.working_table.spec,
                    columns_by_name[column],
                    row_dict(population.content, row_pos),
                )
            pool = resolve_donor_pool(
                state, fork_path, target_kind, current_id, minimum_created_sim_time
            )
            if not pool:
                continue
            eligible_units.append((table_idx, row_pos, column))
            eligible_pools.append(pool)

        if operation.placement is not None:
            row_weights = derive_row_weights(
                operation.placement, placement_populations(populations), rng
            )
            weights = unit_row_weights(eligible_units, row_weights)
            drawn = sorted(draw_weighted_sample(weights, operation.amount, rng))
        else:
            drawn = sorted(draw_sample(len(eligible_units), operation.amount, rng))
        units_selected = len(drawn)

        py_columns_by_table: list[dict[str, list[object]]] = [{} for _ in populations]
        mispoints: list[_Mispoint] = []
        for idx in drawn:
            table_idx, row_pos, column = eligible_units[idx]
            pool = eligible_pools[idx]
            population = populations[table_idx]
            py_columns = py_columns_by_table[table_idx]
            physical_row = population.physical_indices[row_pos]
            # Slot (3): one donor draw per selected unit, ascending
            # selected-unit order, indexing the unit's sorted donor pool.
            donor = pool[rng.randrange(len(pool))]

            if column not in py_columns:
                py_columns[column] = population.working_table.data.column(
                    column
                ).to_pylist()
            py_columns[column][physical_row] = donor

            row = row_dict(population.content, row_pos)
            mispoints.append(_Mispoint(table_idx, column, row, donor))

        write_back_pooled_columns(state, populations, py_columns_by_table)

        defect_class = (
            "point_in_time_dangling_reference"
            if constrained
            else "mispointed_reference"
        )
        defects: list[DefectRecord] = []
        for mispoint in mispoints:
            population = populations[mispoint.table_idx]
            table_spec = population.working_table.spec
            columns_by_name = {col.name: col for col in table_spec.columns}
            record_id = mispoint.row["record_id"]
            assert isinstance(record_id, str)
            impact = mispoint_impact(
                state,
                mispoint.column,
                columns_by_name[mispoint.column],
                table_spec,
                fork_path,
                slice_at,
                record_id,
            )
            row_category = row_category_for_table(table_spec)
            defects.append(
                DefectRecord.model_validate(
                    {
                        "class": defect_class,
                        "rule": rule,
                        "impact": impact,
                        "location": cell_locator(
                            population.table_name,
                            row_category,
                            table_spec,
                            mispoint.row,
                            mispoint.column,
                        ),
                    }
                )
            )

        return OperationOutcome(
            kind="mispoint_reference",
            tables=tuple(table_names),
            units_selected=units_selected,
            units_affected=len(defects),
            defects=tuple(defects),
        )
