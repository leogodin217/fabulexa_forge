"""Emit-dependent business rules for a `CorruptConfig`, checked before any read.

`validate_corrupt_config` resolves each operation's table selector once
against the (static) sidecar table set via `resolve_target_tables`, then
simulates the run's catalog evolution statically — the source sidecar's
`TableSpec`s with each `schema_drift`'s rename / retype / drop folded in, in
operation order — and checks every rule (column-pattern matching, `where`-key
existence, and drift-specific rules) against every resolved table's schema
*as of its position*, so a bad config fails cleanly, before any table is
materialized or written. See
`docs/architecture/pending/corrupter-grammar-v2.md` § Validation Rules and
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Business Rules
— config (normative).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from fabulexa_forge.config.models import (
    ClusteredTemporal,
    Correlated,
    DangleReference,
    DeleteRows,
    DistortIntervals,
    DropEvents,
    DuplicateRows,
    EntityScoped,
    FreezeSeries,
    InsertRows,
    MispointReference,
    MutateCells,
    NullCells,
    SchemaDrift,
    ShiftSimTime,
)
from fabulexa_forge.corrupters.selection import (
    match_column_entries,
    resolve_target_tables,
)
from fabulexa_forge.errors import CorruptValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fabulexa_forge.config.models import CorruptConfig, MutationSpec
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar, TableSpec

# NullableColumns: value-column name patterns eligible for null_cells.
_NULLABLE_EXPLICIT: frozenset[str] = frozenset(
    {"presentation_id", "deactivated_at", "left_sim_time"}
)
_NULLABLE_PREFIXES: tuple[str, ...] = ("prop__", "elem__", "member__")

# JitterColumnsNumeric: numeric-payload types eligible for near-duplicate jitter.
_JITTER_NUMERIC_TYPES: frozenset[str] = frozenset({"BIGINT", "DOUBLE"})

# MutableColumns per-kind type gate: the declared types a mutation kind may
# target, or None for "any".
_MUTATION_TYPE_GATES: dict[str, frozenset[str] | None] = {
    "sentinel": None,
    "typo": frozenset({"VARCHAR", "BIGINT"}),
    "case": frozenset({"VARCHAR"}),
    "whitespace": frozenset({"VARCHAR"}),
    "truncate": frozenset({"VARCHAR"}),
    "precision_drop": frozenset({"DOUBLE"}),
    "scale": frozenset({"BIGINT", "DOUBLE"}),
    "mojibake": frozenset({"VARCHAR"}),
    "format_dirt": frozenset({"VARCHAR"}),
    "resample": None,
    "out_of_domain": frozenset({"VARCHAR"}),
}


def is_nullable_column(name: str) -> bool:
    """NullableColumns: whether `name` is a value column null_cells may target.

    Args:
        name: The column name.

    Returns:
        True iff `name` is a `prop__*` / `elem__*` / `member__*` column, or one
        of the explicit nullable structural-shaped names (presentation_id,
        deactivated_at, left_sim_time).
    """
    return name in _NULLABLE_EXPLICIT or name.startswith(_NULLABLE_PREFIXES)


def is_reference_column(col: "ColumnSpec") -> bool:
    """ReferenceColumns: whether `col` is a reference column dangle_reference
    may target.

    Args:
        col: The column's current (evolved) ColumnSpec.

    Returns:
        True iff `col` is a `member__<f>__id` membership column, or a records
        `prop__` column whose `references` is set.
    """
    if col.name.startswith("member__") and col.name.endswith("__id"):
        return True
    return col.name.startswith("prop__") and col.references is not None


def _is_enum_discriminator(
    col_name: str, spec: "TableSpec", sidecar: "Sidecar"
) -> bool:
    """Whether `col_name` is the `prop__<kind>_type` sub-type discriminator
    backing an `enum_domains` entry for `spec`'s record kind.

    Args:
        col_name: The column name (as of this operation's position).
        spec: The table's current (evolved) TableSpec.
        sidecar: The source sidecar (enum_domains is copied verbatim by the
            writer, so its declared shape never drifts).

    Returns:
        True iff `col_name` is exactly `prop__<record_kind>_type` and
        `enum_domains` declares that discriminator for the table's kind.
    """
    kind = spec.record_kind
    if kind is None or col_name != f"prop__{kind}_type":
        return False
    return f"{kind}_type" in sidecar.enum_domains().get(kind, {})


def _is_drift_eligible(
    col: "ColumnSpec", spec: "TableSpec", sidecar: "Sidecar"
) -> bool:
    """DriftColumnsNonStructural: whether `col` is a payload column
    schema_drift may rename/retype/drop.

    Positively enumerated: a records `prop__*` column with `references` unset
    that is not the `enum_domains` sub-type discriminator, or a membership
    `elem__*` column. Every other column — the structural prefixes, all
    `history` columns, every reference column, and the discriminator — is
    ineligible.

    Args:
        col: The column's current (evolved) ColumnSpec.
        spec: The table's current (evolved) TableSpec.
        sidecar: The source sidecar, for the enum_domains discriminator check.

    Returns:
        True iff `col` is drift-eligible.
    """
    if col.name.startswith("prop__") and col.references is None:
        return not _is_enum_discriminator(col.name, spec, sidecar)
    return col.name.startswith("elem__")


def _drift_category(name: str) -> str | None:
    """The schema_drift structural category a column name belongs to.

    Args:
        name: A column name.

    Returns:
        "prop__" for a records prop__* column, "elem__" for a membership
        elem__* column, None otherwise.
    """
    if name.startswith("prop__"):
        return "prop__"
    if name.startswith("elem__"):
        return "elem__"
    return None


def is_jitter_eligible(col: "ColumnSpec") -> bool:
    """JitterColumnsNumeric: whether `col` is a numeric payload column
    near-duplicate jitter may perturb.

    Args:
        col: The column's current (evolved) ColumnSpec.

    Returns:
        True iff `col` is a `prop__*` / `elem__*` column, is not a `*_sim_time`
        lifecycle column, is not a records reference `prop__` column (declared
        ineligible regardless of its DuckDB type -- never a numeric-type
        coincidence), and its DuckDB type is BIGINT or DOUBLE.
    """
    if col.name.endswith("_sim_time"):
        return False
    if col.name.startswith("prop__") and col.references is not None:
        return False
    if not (col.name.startswith("prop__") or col.name.startswith("elem__")):
        return False
    return col.type.upper() in _JITTER_NUMERIC_TYPES


def is_mutable_column(name: str, category: str, references: str | None) -> bool:
    """MutableColumns name class: whether a column is family-A-eligible by name.

    Args:
        name: The column name (as of the operation's position).
        category: The table's category ("records" / "membership" / "fixed").
        references: The column's current ColumnSpec.references (None when the
            column is not a declared reference).

    Returns:
        True iff the column is a records `prop__*` with `references` unset, a
        records `presentation_id`, a membership `elem__*`, or the `value`
        column of a fixed-category table -- at `base_format_version` 4 that is
        exactly `history.value`, since `history` is the contract's sole
        fixed-category table.
    """
    if category == "records":
        if name == "presentation_id":
            return True
        return name.startswith("prop__") and references is None
    if category == "membership":
        return name.startswith("elem__")
    return name == "value"


def _is_out_of_domain_eligible(
    col_name: str, spec: "TableSpec", sidecar: "Sidecar"
) -> bool:
    """OutOfDomainEligible: whether `col_name` is a records `prop__<p>`
    column whose (kind, p) pair the sidecar declares in `enum_domains`.

    Args:
        col_name: The column name (as of this operation's position).
        spec: The table's current (evolved) TableSpec.
        sidecar: The source sidecar, for the enum_domains lookup.

    Returns:
        True iff `col_name` is `prop__<p>` and `enum_domains[spec.record_kind]`
        declares `p`.
    """
    if not col_name.startswith("prop__"):
        return False
    kind = spec.record_kind
    if kind is None:
        return False
    property_name = col_name[len("prop__") :]
    return property_name in sidecar.enum_domains().get(kind, {})


def mutation_eligible_columns(
    mutation: "MutationSpec", spec: "TableSpec", sidecar: "Sidecar"
) -> list[str]:
    """The columns of one resolved table this mutation kind may target.

    Intersects the MutableColumns name class with the mutation kind's type
    gate (per the eligibility matrix) against `spec`'s current column types;
    for `out_of_domain`, additionally requires a records `prop__<p>` column
    whose kind/property pair is declared in `sidecar.enum_domains()`.

    Args:
        mutation: The operation's mutation spec.
        spec: The resolved table's current (evolved) TableSpec.
        sidecar: The source sidecar (enum_domains is copied verbatim by the
            writer, so its declared shape never drifts).

    Returns:
        Eligible column names in `spec` column order (the match domain for
        `target.columns` entries).
    """
    gate = _MUTATION_TYPE_GATES[mutation.kind]
    eligible = [
        col.name
        for col in spec.columns
        if is_mutable_column(col.name, spec.category, col.references)
        and (gate is None or col.type.upper() in gate)
    ]
    if mutation.kind != "out_of_domain":
        return eligible
    return [
        name for name in eligible if _is_out_of_domain_eligible(name, spec, sidecar)
    ]


def conflict_eligible_columns(
    mutation: "MutationSpec", spec: "TableSpec", sidecar: "Sidecar"
) -> list[str]:
    """The columns of one resolved table `duplicate_rows.mutation` may transform.

    A fixed-category table has no conflict-eligible columns (`history.value`
    is deliberately excluded -- design doc § Semantics, the mutation mode);
    otherwise delegates to `mutation_eligible_columns` (the records/membership
    members of the MutableColumns name class, narrowed by the mutation kind's
    type gate and, for `out_of_domain`, the `enum_domains` gate).

    Args:
        mutation: The operation's mutation spec.
        spec: The resolved table's current (evolved) TableSpec.
        sidecar: The source sidecar, for the out_of_domain enum-domain gate.

    Returns:
        Eligible column names in `spec` column order.
    """
    if spec.category == "fixed":
        return []
    return mutation_eligible_columns(mutation, spec, sidecar)


def insert_eligible_columns(spec: "TableSpec") -> list[str]:
    """The columns of one resolved records table `insert_rows` may resample.

    Args:
        spec: The resolved table's current (evolved) TableSpec.

    Returns:
        The insert-eligible column names in `spec` column order: records
        `prop__*` columns with `references` unset, plus `presentation_id` --
        the match domain for `insert_rows.target.columns` entries.
    """
    return [
        col.name
        for col in spec.columns
        if is_mutable_column(col.name, spec.category, col.references)
    ]


def _check_columns_exist(
    spec: "TableSpec", columns: "Iterable[str]", op_index: int
) -> None:
    """ColumnsExist: every name in `columns` is a column of `spec` as of this
    operation's position. Retained for `schema_drift`'s exact-name
    rename_to / retype_to / drop maps; `target.columns` existence now goes
    through `ColumnEntriesMatch`.

    Args:
        spec: The table's current (evolved) TableSpec.
        columns: The column names the operation names.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A name in `columns` is not a current column.
    """
    names = {col.name for col in spec.columns}
    for col in columns:
        if col not in names:
            raise CorruptValidationError(
                f"operation[{op_index}]: table {spec.name!r} has no column {col!r}"
            )


def _check_where_columns_exist(
    resolved_specs: "Sequence[TableSpec]",
    where: "Mapping[str, str] | None",
    op_index: int,
) -> None:
    """WhereColumnsExist (generalized): every `target.where` key is a current
    column of >= 1 resolved table.

    Args:
        resolved_specs: The operation's resolved tables' current TableSpecs.
        where: The operation's target.where, or None.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A `where` key is a column of no resolved table.
    """
    if not where:
        return
    for col in where:
        if not any(col in {c.name for c in spec.columns} for spec in resolved_specs):
            raise CorruptValidationError(
                f"operation[{op_index}]: where names unknown column {col!r}"
                " in any resolved table"
            )


def _check_history_only_target(
    resolved_names: "Sequence[str]", op_index: int, kind: str
) -> None:
    """HistoryOnlyTarget: a family-C operation's target resolves to the
    fixed-category `history` table only.

    Args:
        resolved_names: The operation's resolved table names.
        op_index: The operation's 0-based position, for the error message.
        kind: The operation's `kind` literal, for the error message.

    Raises:
        CorruptValidationError: A resolved table other than `history` is
            present (including the all-history-plus-another-table case).
    """
    if list(resolved_names) != ["history"]:
        raise CorruptValidationError(
            f"operation {op_index} ({kind}): target must resolve to the"
            f" history table only; resolved {list(resolved_names)}"
        )


def _check_non_history_target(
    resolved_specs: "Sequence[TableSpec]", op_index: int
) -> None:
    """NonHistoryTarget: every resolved table of a `delete_rows` operation is
    records- or membership-category — never fixed-category (`history`
    removal is `drop_events`' alone).

    Args:
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A resolved table is fixed-category.
    """
    for spec in resolved_specs:
        if spec.category == "fixed":
            raise CorruptValidationError(
                f"operation[{op_index}]: delete_rows target resolves to"
                f" fixed-category table {spec.name!r}; history removal is"
                " drop_events' alone"
            )


def _check_records_category_target(
    resolved_specs: "Sequence[TableSpec]", op_index: int
) -> None:
    """RecordsCategoryTarget: every resolved table of an `insert_rows`
    operation is records-category.

    Args:
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A resolved table is not records-category.
    """
    for spec in resolved_specs:
        if spec.category != "records":
            raise CorruptValidationError(
                f"operation[{op_index}]: insert_rows target resolves to"
                f" non-records-category table {spec.name!r}"
            )


def _check_membership_only_target(
    resolved_specs: "Sequence[TableSpec]", op_index: int
) -> None:
    """MembershipOnlyTarget: every resolved table of a `distort_intervals`
    operation is membership-category -- `history` and records-category
    tables have no membership intervals.

    Args:
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A resolved table is not membership-category.
    """
    for spec in resolved_specs:
        if spec.category != "membership":
            raise CorruptValidationError(
                f"operation {op_index} (distort_intervals): target must"
                f" resolve to membership-category tables only; got {spec.name!r}"
            )


def _check_phantom_resample_columns(
    operation: "InsertRows",
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
) -> None:
    """PhantomResampleColumns: with `target.columns` present, every entry
    matches >= 1 insert-eligible column in >= 1 resolved table -- a dead
    entry is a misconfiguration.

    Args:
        operation: The parsed insert_rows operation (target.columns present).
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: An entry matches zero insert-eligible columns
            in every resolved table.
    """
    entries = operation.target.columns
    assert entries is not None
    per_table_eligible = [insert_eligible_columns(spec) for spec in resolved_specs]
    for entry in entries:
        if not any(
            match_column_entries([entry], eligible) for eligible in per_table_eligible
        ):
            raise CorruptValidationError(
                f"operation[{op_index}]: columns entry {entry!r} matches no"
                " insert-eligible column in any resolved table"
            )


def _eligible_columns_for_operation(
    operation: "NullCells | DuplicateRows | DangleReference | MispointReference",
    spec: "TableSpec",
) -> list[str]:
    """The operation's eligible working columns of `spec` — the match domain
    `ColumnEntriesMatch` matches `target.columns` entries against.

    Args:
        operation: The parsed operation (target.columns present).
        spec: One resolved table's current (evolved) TableSpec.

    Returns:
        NullableColumns for `null_cells`, ReferenceColumns for
        `dangle_reference` / `mispoint_reference`, JitterColumnsNumeric for
        jitter-mode `duplicate_rows` (mutation mode routes through
        `_check_conflict_mutable_columns` instead, never reaching here).
    """
    if isinstance(operation, NullCells):
        return [col.name for col in spec.columns if is_nullable_column(col.name)]
    if isinstance(operation, DangleReference | MispointReference):
        return [col.name for col in spec.columns if is_reference_column(col)]
    assert operation.jitter is not None
    return [col.name for col in spec.columns if is_jitter_eligible(col)]


def _check_column_entries_match(
    operation: "NullCells | DuplicateRows | DangleReference | MispointReference",
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
) -> None:
    """ColumnEntriesMatch: every `target.columns` entry matches >= 1
    operation-eligible column in >= 1 resolved table.

    Args:
        operation: The parsed operation (target.columns present).
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: An entry matches zero eligible columns in
            every resolved table.
    """
    entries = operation.target.columns
    assert entries is not None
    per_table_eligible = [
        _eligible_columns_for_operation(operation, spec) for spec in resolved_specs
    ]
    for entry in entries:
        if not any(
            match_column_entries([entry], eligible) for eligible in per_table_eligible
        ):
            raise CorruptValidationError(
                f"operation[{op_index}]: columns entry {entry!r} matches no"
                " eligible column in any resolved table"
            )


def _check_mutable_columns(
    operation: MutateCells,
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
    sidecar: "Sidecar",
) -> None:
    """MutableColumns: every `target.columns` entry matches >= 1
    mutation-eligible column in >= 1 resolved table.

    Args:
        operation: The parsed mutate_cells operation (target.columns present).
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.
        sidecar: The source sidecar, for the out_of_domain enum-domain gate.

    Raises:
        CorruptValidationError: An entry matches zero eligible columns in
            every resolved table.
    """
    entries = operation.target.columns
    assert entries is not None
    per_table_eligible = [
        mutation_eligible_columns(operation.mutation, spec, sidecar)
        for spec in resolved_specs
    ]
    for entry in entries:
        if not any(
            match_column_entries([entry], eligible) for eligible in per_table_eligible
        ):
            raise CorruptValidationError(
                f"operation {op_index} (mutate_cells): columns entry {entry!r}"
                f" matches no {operation.mutation.kind}-eligible column in any"
                " resolved table"
            )


def _check_conflict_mutable_columns(
    operation: "DuplicateRows",
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
    sidecar: "Sidecar",
) -> None:
    """ConflictMutableColumns: with `mutation` present, every `target.columns`
    entry matches >= 1 conflict-eligible column in >= 1 resolved table.

    Args:
        operation: The parsed duplicate_rows operation (mutation present,
            target.columns present).
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.
        sidecar: The source sidecar, for the out_of_domain enum-domain gate.

    Raises:
        CorruptValidationError: An entry matches zero conflict-eligible
            columns in every resolved table.
    """
    entries = operation.target.columns
    assert entries is not None
    mutation = operation.mutation
    assert mutation is not None
    per_table_eligible = [
        conflict_eligible_columns(mutation, spec, sidecar) for spec in resolved_specs
    ]
    for entry in entries:
        if not any(
            match_column_entries([entry], eligible) for eligible in per_table_eligible
        ):
            raise CorruptValidationError(
                f"operation {op_index} (duplicate_rows): columns entry {entry!r}"
                f" matches no {mutation.kind}-eligible column in any"
                " resolved table"
            )


def _check_placement_column_exists(
    column: str,
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
    *,
    require_bigint: bool,
) -> None:
    """PlacementColumnExists: `column` (correlated.column /
    clustered_temporal.column) is a current column of >= 1 resolved table;
    when `require_bigint`, it is BIGINT in every resolved table that has it.

    Args:
        column: The placement's condition/sim-time column name.
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.
        require_bigint: Whether `column` must be BIGINT wherever present
            (clustered_temporal only).

    Raises:
        CorruptValidationError: `column` is a column of no resolved table, or
            (when `require_bigint`) is non-BIGINT in a resolved table that
            has it.
    """
    found = False
    for spec in resolved_specs:
        col = next((c for c in spec.columns if c.name == column), None)
        if col is None:
            continue
        found = True
        if require_bigint and col.type.upper() != "BIGINT":
            raise CorruptValidationError(
                f"operation[{op_index}]: placement column {column!r} must be"
                f" BIGINT in table {spec.name!r}; got {col.type!r}"
            )
    if not found:
        raise CorruptValidationError(
            f"operation[{op_index}]: placement column {column!r} is not a"
            " column of any resolved table"
        )


def _check_entity_scoped_record_id(
    resolved_specs: "Sequence[TableSpec]", op_index: int
) -> None:
    """EntityScopedRecordId: with entity_scoped, record_id is a current
    column of every resolved table.

    Args:
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A resolved table lacks record_id.
    """
    for spec in resolved_specs:
        if not any(col.name == "record_id" for col in spec.columns):
            raise CorruptValidationError(
                f"operation[{op_index}]: entity_scoped requires record_id in"
                f" table {spec.name!r}"
            )


def _check_placement(
    placement: "EntityScoped | ClusteredTemporal | Correlated | None",
    resolved_specs: "Sequence[TableSpec]",
    op_index: int,
) -> None:
    """Check the business rule for the operation's `placement`, if present.

    Args:
        placement: The operation's placement config, or None.
        resolved_specs: The operation's resolved tables' current TableSpecs.
        op_index: The operation's 0-based position, for error messages.

    Raises:
        CorruptValidationError: PlacementColumnExists or EntityScopedRecordId
            fails.
    """
    if placement is None:
        return
    if isinstance(placement, EntityScoped):
        _check_entity_scoped_record_id(resolved_specs, op_index)
        return
    if isinstance(placement, ClusteredTemporal):
        _check_placement_column_exists(
            placement.column, resolved_specs, op_index, require_bigint=True
        )
        return
    _check_placement_column_exists(
        placement.column, resolved_specs, op_index, require_bigint=False
    )


def _check_rename_preserves_category(
    rename_to: "Mapping[str, str]", op_index: int
) -> None:
    """DriftRenamePreservesCategory: a rename target keeps the source column's
    structural category (prop__* -> prop__*, elem__* -> elem__*).

    Args:
        rename_to: The schema_drift operation's rename_to map.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: A rename target changes the column's category.
    """
    for old, new in rename_to.items():
        if _drift_category(old) != _drift_category(new):
            raise CorruptValidationError(
                f"operation[{op_index}]: rename target {new!r} changes {old!r}'s"
                " column category; schema_drift preserves C1-C5"
            )


def _apply_drift_to_spec(spec: "TableSpec", operation: SchemaDrift) -> "TableSpec":
    """Fold one schema_drift operation's rename/retype/drop into an evolved spec.

    The rename_to / retype_to / drop maps resolve against `spec` (the
    pre-operation schema) and apply as one atomic, set-semantics transform, so
    their declaration order does not affect the result.

    Args:
        spec: The table's schema before this operation.
        operation: The parsed schema_drift operation.

    Returns:
        The table's schema after this operation.
    """
    rename_to = operation.rename_to or {}
    retype_to = operation.retype_to or {}
    drop = set(operation.drop or [])
    new_columns = tuple(
        replace(
            col,
            name=rename_to.get(col.name, col.name),
            type=retype_to.get(col.name, col.type),
        )
        for col in spec.columns
        if col.name not in drop
    )
    return replace(spec, columns=new_columns)


def _check_drift_no_target_collision(evolved_spec: "TableSpec", op_index: int) -> None:
    """DriftNoTargetCollision: the evolved catalog names no column twice.

    Catches both a rename map with two sources sharing a target name, and a
    rename target that collides with a surviving (untouched) column — either
    would leave the evolved catalog with a duplicate column name. A pure
    function of column names, so it belongs in the evolved-schema simulation;
    `schema_drift.py`'s apply-time check remains as a backstop.

    Args:
        evolved_spec: The table's TableSpec after folding this drift in.
        op_index: The operation's 0-based position, for the error message.

    Raises:
        CorruptValidationError: Two or more columns of `evolved_spec` share a
            name.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for col in evolved_spec.columns:
        if col.name in seen:
            duplicates.add(col.name)
        seen.add(col.name)
    if duplicates:
        raise CorruptValidationError(
            f"operation[{op_index}]: schema_drift rename produces colliding"
            f" column name(s) {sorted(duplicates)!r} in {evolved_spec.name!r}"
        )


def _validate_schema_drift(
    spec: "TableSpec", operation: SchemaDrift, op_index: int, sidecar: "Sidecar"
) -> "TableSpec":
    """Check ColumnsExist, DriftColumnsNonStructural,
    DriftRenamePreservesCategory, and DriftNoTargetCollision, then fold the
    drift into the evolved schema.

    Args:
        spec: The table's current (evolved) TableSpec.
        operation: The parsed schema_drift operation.
        op_index: The operation's 0-based position, for error messages.
        sidecar: The source sidecar, for the enum_domains discriminator check.

    Returns:
        The table's evolved TableSpec after this operation's drift.

    Raises:
        CorruptValidationError: ColumnsExist, DriftColumnsNonStructural,
            DriftRenamePreservesCategory, or DriftNoTargetCollision fails.
    """
    rename_to = operation.rename_to or {}
    retype_to = operation.retype_to or {}
    drop = operation.drop or []
    touched = [*rename_to, *retype_to, *drop]
    _check_columns_exist(spec, touched, op_index)
    columns_by_name = {col.name: col for col in spec.columns}
    for col_name in touched:
        if not _is_drift_eligible(columns_by_name[col_name], spec, sidecar):
            raise CorruptValidationError(
                f"operation[{op_index}]: {col_name!r} is not a drift-eligible"
                " payload column; schema_drift touches only non-reference"
                " prop__ / elem__ columns and preserves C1-C5, C10, C12"
            )
    _check_rename_preserves_category(rename_to, op_index)
    evolved = _apply_drift_to_spec(spec, operation)
    _check_drift_no_target_collision(evolved, op_index)
    return evolved


def validate_corrupt_config(config: "CorruptConfig", sidecar: "Sidecar") -> None:
    """Check the emit-dependent business rules for every operation; raise on
    first fail.

    Per operation, the selector is resolved once via `resolve_target_tables`
    against the (static) sidecar table set (`SelectorResolves`); every other
    rule then evaluates against the resolved table set's simulated schema —
    the source sidecar's TableSpecs with each schema_drift's rename / retype /
    drop folded in, in operation order (the same spec evolution WorkingTable.
    spec undergoes at apply time, computable without data because drift is
    config-only). `ColumnEntriesMatch` matches target.columns entries (exact
    or fnmatch pattern) against each resolved table's operation-eligible
    columns; `WhereColumnsExist` and `schema_drift`'s exact-name ColumnsExist
    and evolved-catalog `DriftNoTargetCollision` round out the table. When
    `placement` is present, `PlacementColumnExists`
    (correlated / clustered_temporal) or `EntityScopedRecordId` checks it
    against the same resolved-table set. A bad config fails cleanly, before
    any table is read or written. The one data-dependent check —
    schema_drift retype validity — cannot be checked here (the cast needs the
    actual data); Corrupter.apply raises for that.

    Args:
        config: The parsed corrupter config.
        sidecar: The open emit's sidecar.

    Raises:
        CorruptValidationError: The first failing business rule, naming the
            operation index, the table/column, and the rule violated.
    """
    schema: dict[str, TableSpec] = {table.name: table for table in sidecar.tables()}
    for op_index, operation in enumerate(config.operations):
        try:
            resolved_names = resolve_target_tables(operation.target, sidecar)
        except CorruptValidationError as exc:
            raise CorruptValidationError(f"operation[{op_index}]: {exc}") from exc
        if isinstance(operation, SchemaDrift):
            table_name = resolved_names[0]
            schema[table_name] = _validate_schema_drift(
                schema[table_name], operation, op_index, sidecar
            )
            continue
        resolved_specs = [schema[name] for name in resolved_names]
        _check_where_columns_exist(resolved_specs, operation.target.where, op_index)
        if isinstance(operation, DropEvents | FreezeSeries | ShiftSimTime):
            _check_history_only_target(resolved_names, op_index, operation.kind)
        elif isinstance(operation, DuplicateRows):
            if operation.mutation is not None:
                _check_conflict_mutable_columns(
                    operation, resolved_specs, op_index, sidecar
                )
            elif operation.jitter is not None:
                _check_column_entries_match(operation, resolved_specs, op_index)
        elif isinstance(operation, MutateCells):
            _check_mutable_columns(operation, resolved_specs, op_index, sidecar)
        elif isinstance(operation, DeleteRows):
            _check_non_history_target(resolved_specs, op_index)
        elif isinstance(operation, InsertRows):
            _check_records_category_target(resolved_specs, op_index)
            if operation.target.columns is not None:
                _check_phantom_resample_columns(operation, resolved_specs, op_index)
        elif isinstance(operation, DistortIntervals):
            _check_membership_only_target(resolved_specs, op_index)
        else:
            _check_column_entries_match(operation, resolved_specs, op_index)
        _check_placement(operation.placement, resolved_specs, op_index)
