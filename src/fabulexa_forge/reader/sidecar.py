"""Typed sidecar parse layer for base-layer emits.

Provides Sidecar.from_raw: version-gate + structural floor over a parsed base.json
mapping. Pure Python — no files, no DuckDB.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence, cast

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.errors import (
    ColumnNotFoundError,
    PresentationKeysInvalidError,
    SidecarStructureError,
    TableNotFoundError,
    TemporalClassUnavailableError,
    UnsupportedBaseFormatVersionError,
)

TemporalClass = Literal["constant", "tracked", "slice_only"]
"""The point-in-time contract for one value-carrying column, read from the sidecar.

Never inferred: a column that declares no class has no class, and a surface that
needs one refuses rather than deriving it from history_tracked.
"""

_TEMPORAL_CLASSES: frozenset[str] = frozenset({"constant", "tracked", "slice_only"})

#: The contract's closed table-category enum. Restates the vendored schema's
#: `category` enum — contract-pinned, the same hardcoding class as the pinned
#: column lists. An out-of-set value refuses at parse (design doc § The
#: structural-temporal surface).
_TABLE_CATEGORIES: frozenset[str] = frozenset({"fixed", "records", "membership"})


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a base-layer table, as declared in base.json."""

    name: str
    type: str  # DuckDB type literal, e.g. "BIGINT", "VARCHAR"
    references: str | None  # FK target record kind, or None when not a reference column
    # C11 flag: True/False when carried; None when the emit predates it
    # (all-or-none per emit)
    history_tracked: bool | None
    # Declared verbatim, never validated/coerced at parse (C13's enum clause needs to
    # see an out-of-enum value); absent -> None. Narrows to TemporalClass only through
    # Sidecar.temporal_class.
    temporal_class: str | None


@dataclass(frozen=True)
class TableSpec:
    """One table present in run.duckdb, as declared in base.json."""

    name: str
    category: str  # "fixed" | "records" | "membership"
    record_kind: str | None  # the kind for records/membership; None when absent.
    # Schema-required for records/membership (conditional-required) — C1 enforces.
    property: str | None  # the membership property name; None when absent.
    # Schema-required for membership — C1 enforces.
    columns: tuple[ColumnSpec, ...]
    rows: int


@dataclass(frozen=True)
class BranchEntry:
    """One branch present in the emit."""

    fork_path: str
    parent: str | None  # @-joined parent path, or None for the root branch
    slice_at: int  # sim_time this branch was sliced at


@dataclass(frozen=True)
class RuntimeAnchor:
    """Run-level wallclock anchor for sim_time = 0."""

    timezone: str  # IANA timezone string
    start_datetime: str  # ISO-8601 tz-aware datetime, raw from the sidecar


def _parse_column(raw_col: object, table_name: str, col_idx: int) -> ColumnSpec:
    """Parse a single column entry from a table's columns list.

    Args:
        raw_col: The raw column object from the sidecar.
        table_name: The table name, for error messages.
        col_idx: The column index, for error messages.

    Returns:
        A ColumnSpec.

    Raises:
        SidecarStructureError: The column is not a mapping or is missing name/type.
    """
    if not isinstance(raw_col, dict):
        raise SidecarStructureError(
            f"table '{table_name}' column[{col_idx}] is not an object"
        )
    col_name = raw_col.get("name")
    if not isinstance(col_name, str):
        raise SidecarStructureError(
            f"table '{table_name}' column[{col_idx}] missing or non-string 'name'"
        )
    col_type = raw_col.get("type")
    if not isinstance(col_type, str):
        raise SidecarStructureError(
            f"table '{table_name}' column[{col_idx}] missing or non-string 'type'"
        )
    references_raw = raw_col.get("references")
    references: str | None = references_raw if isinstance(references_raw, str) else None
    history_tracked_raw = raw_col.get("history_tracked")
    history_tracked: bool | None = (
        history_tracked_raw if isinstance(history_tracked_raw, bool) else None
    )
    temporal_class_raw = raw_col.get("temporal_class")
    temporal_class: str | None = (
        temporal_class_raw if isinstance(temporal_class_raw, str) else None
    )
    return ColumnSpec(
        name=col_name,
        type=col_type,
        references=references,
        history_tracked=history_tracked,
        temporal_class=temporal_class,
    )


def _parse_table(raw_table: object, table_idx: int) -> TableSpec:
    """Parse a single table entry from the sidecar tables list.

    Args:
        raw_table: The raw table object from the sidecar.
        table_idx: The table index, for error messages.

    Returns:
        A TableSpec.

    Raises:
        SidecarStructureError: The table is not a mapping or is missing required fields.
    """
    if not isinstance(raw_table, dict):
        raise SidecarStructureError(f"tables[{table_idx}] is not an object")

    name_raw = raw_table.get("name")
    if not isinstance(name_raw, str):
        raise SidecarStructureError(f"tables[{table_idx}] missing or non-string 'name'")
    name: str = name_raw

    category_raw = raw_table.get("category")
    if not isinstance(category_raw, str):
        raise SidecarStructureError(f"table '{name}' missing or non-string 'category'")
    if category_raw not in _TABLE_CATEGORIES:
        raise SidecarStructureError(
            f"table '{name}' unrecognised category '{category_raw}'"
        )

    columns_raw = raw_table.get("columns")
    if not isinstance(columns_raw, list):
        raise SidecarStructureError(f"table '{name}' missing or non-list 'columns'")

    rows_raw = raw_table.get("rows")
    if not isinstance(rows_raw, int) or isinstance(rows_raw, bool):
        raise SidecarStructureError(f"table '{name}' missing or non-integer 'rows'")

    columns = tuple(
        _parse_column(col, name, idx) for idx, col in enumerate(columns_raw)
    )

    record_kind_raw = raw_table.get("record_kind")
    record_kind: str | None = (
        record_kind_raw if isinstance(record_kind_raw, str) else None
    )

    property_raw = raw_table.get("property")
    property_val: str | None = property_raw if isinstance(property_raw, str) else None

    return TableSpec(
        name=name,
        category=category_raw,
        record_kind=record_kind,
        property=property_val,
        columns=columns,
        rows=rows_raw,
    )


def _parse_branch(raw_branch: object, branch_idx: int) -> BranchEntry:
    """Parse a single branch entry from the sidecar branches list.

    Args:
        raw_branch: The raw branch object from the sidecar.
        branch_idx: The branch index, for error messages.

    Returns:
        A BranchEntry.

    Raises:
        SidecarStructureError: The branch is not a mapping or missing required fields.
    """
    if not isinstance(raw_branch, dict):
        raise SidecarStructureError(f"branches[{branch_idx}] is not an object")

    fork_path_raw = raw_branch.get("fork_path")
    if not isinstance(fork_path_raw, str):
        raise SidecarStructureError(
            f"branches[{branch_idx}] missing or non-string 'fork_path'"
        )

    if "parent" not in raw_branch:
        raise SidecarStructureError(
            f"branches[{branch_idx}] '{fork_path_raw}' is missing 'parent' key"
        )
    parent_raw = raw_branch["parent"]
    if parent_raw is not None and not isinstance(parent_raw, str):
        raise SidecarStructureError(
            f"branches[{branch_idx}] 'parent' must be a string or null"
        )
    parent: str | None = parent_raw

    slice_at_raw = raw_branch.get("slice_at")
    if not isinstance(slice_at_raw, int) or isinstance(slice_at_raw, bool):
        raise SidecarStructureError(
            f"branches[{branch_idx}] missing or non-integer 'slice_at'"
        )

    return BranchEntry(
        fork_path=fork_path_raw,
        parent=parent,
        slice_at=slice_at_raw,
    )


def _parse_runtime(raw_runtime: object) -> RuntimeAnchor | None:
    """Parse the optional runtime block from the sidecar.

    Args:
        raw_runtime: The raw runtime value from the sidecar (may be absent/None).

    Returns:
        A RuntimeAnchor if present and parseable, None otherwise.
    """
    if raw_runtime is None:
        return None
    if not isinstance(raw_runtime, dict):
        return None
    timezone = raw_runtime.get("timezone")
    start_datetime = raw_runtime.get("start_datetime")
    if not isinstance(timezone, str) or not isinstance(start_datetime, str):
        return None
    return RuntimeAnchor(timezone=timezone, start_datetime=start_datetime)


def _parse_pinned_ids(
    raw: object,
) -> Mapping[str, Mapping[str, str]]:
    """Parse the optional pinned_ids block into a nested mapping.

    Args:
        raw: The raw pinned_ids value from the sidecar.

    Returns:
        A nested {kind: {label: id}} mapping, empty when absent or invalid.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for kind, labels in raw.items():
        if isinstance(labels, dict):
            inner: dict[str, str] = {}
            for label, id_val in labels.items():
                if isinstance(id_val, str):
                    inner[label] = id_val
            result[kind] = inner
    return result


def _parse_enum_domains(
    raw: object,
) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    """Parse the optional enum_domains block into a nested mapping.

    Args:
        raw: The raw enum_domains value from the sidecar.

    Returns:
        A nested {kind: {property: (option, ...)}} mapping, empty when absent.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for kind, props in raw.items():
        if isinstance(props, dict):
            inner: dict[str, tuple[str, ...]] = {}
            for prop, options in props.items():
                if isinstance(options, list):
                    inner[prop] = tuple(str(o) for o in options if isinstance(o, str))
            result[kind] = inner
    return result


@dataclass(frozen=True)
class SeriesCensus:
    """Row and distinct-record counts for one (kind, property) history series."""

    rows: int
    records: int


@dataclass(frozen=True)
class BranchCensus:
    """One branch's row counts, from the sidecar's optional `row_census` block.

    Counts of emitted rows and of distinct record identities — never an aggregate
    over values. Advisory: no conformance check ranges over the block, so every
    consumer reads it as evidence and must carry a path for its absence.
    """

    table_rows: Mapping[str, int]
    history_series: Mapping[str, Mapping[str, SeriesCensus]]
    sub_type_rows: Mapping[str, Mapping[str, int]]


def _parse_count_map(raw: object) -> dict[str, int]:
    """Parse a {name: row_count} census object, dropping non-count entries.

    Args:
        raw: The raw count-map value from the sidecar.

    Returns:
        A {name: count} mapping, empty when absent or malformed.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        name: count
        for name, count in raw.items()
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }


def _parse_history_series(raw: object) -> dict[str, dict[str, SeriesCensus]]:
    """Parse the census's history_series sub-block.

    Args:
        raw: The raw history_series value from the sidecar.

    Returns:
        A nested {kind: {property: SeriesCensus}} mapping, empty when absent. A
        series is enumerated only when observed, so absence means zero rows.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, SeriesCensus]] = {}
    for kind, props in raw.items():
        if not isinstance(props, dict):
            continue
        inner: dict[str, SeriesCensus] = {}
        for prop, counts in props.items():
            if not isinstance(counts, dict):
                continue
            rows = counts.get("rows")
            records = counts.get("records")
            if (
                isinstance(rows, int)
                and not isinstance(rows, bool)
                and isinstance(records, int)
                and not isinstance(records, bool)
            ):
                inner[prop] = SeriesCensus(rows=rows, records=records)
        result[kind] = inner
    return result


def _parse_row_census(raw: object, fork_path: str) -> BranchCensus | None:
    """Parse the optional row_census block for one branch.

    Args:
        raw: The raw row_census value from the sidecar.
        fork_path: The branch whose census to read — the emit's single branch
            (a sanitised emit carries exactly one; C8 asserts it).

    Returns:
        The BranchCensus for fork_path, or None when the block is absent or
        carries no entry for this branch. Absence is the block's declared
        optional posture, never an error.
    """
    if not isinstance(raw, dict):
        return None
    branch = raw.get(fork_path)
    if not isinstance(branch, dict):
        return None
    sub_type_raw = branch.get("sub_type_rows")
    sub_type_rows = (
        {
            kind: _parse_count_map(split)
            for kind, split in sub_type_raw.items()
            if isinstance(split, dict)
        }
        if isinstance(sub_type_raw, dict)
        else {}
    )
    return BranchCensus(
        table_rows=_parse_count_map(branch.get("table_rows")),
        history_series=_parse_history_series(branch.get("history_series")),
        sub_type_rows=sub_type_rows,
    )


@dataclass(frozen=True)
class RecordRoles:
    """Typed view of the sidecar `record_roles` registry.

    Wraps the per-kind warehouse-role taxonomy from `base.json`. Each kind maps
    either to a bare role string ("dimension" or "fact") or, for an
    object-valued kind (today only `actor`), to a {sub_type: role} object. The
    view owns the contract's asymmetric read rule so no consumer re-derives the
    object-vs-string branch (reader-first). Built from the sidecar; never
    re-exported from a producer type.
    """

    # _registry stores either a str (bare role) or dict[str, str] (sub_type -> role)
    _registry: Mapping[str, str | Mapping[str, str]]

    def kinds(self) -> tuple[str, ...]:
        """The registered kind names.

        Returns:
            Kind names in sidecar order — which the contract guarantees is
            lexicographic. The accessor trusts that order rather than
            re-sorting defensively (faithful read).
        """
        return tuple(self._registry.keys())

    def is_subtyped(self, kind: str) -> bool:
        """Whether a kind's role is resolved per sub-type.

        A caller uses this to decide whether it must read the row's
        `prop__<kind>_type` discriminator before resolving a role.

        Args:
            kind: A records-category kind name.

        Returns:
            True iff `record_roles[kind]` is an object keyed by sub-type; False
            iff it is a bare role string.

        Raises:
            KeyError: `kind` is not in the registry.
        """
        entry = self._registry[kind]  # raises KeyError when absent
        return isinstance(entry, Mapping)

    def sub_types(self, kind: str) -> tuple[str, ...]:
        """The declared sub-types for an object-valued (subtyped) kind.

        Args:
            kind: A records-category kind name.

        Returns:
            Sub-type names in registry/declaration order.

        Raises:
            KeyError: `kind` is not in the registry.
            ValueError: `kind` is a bare-string (non-subtyped) kind and has no
                enumerable sub-types.
        """
        entry = self._registry[kind]  # raises KeyError when absent
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"kind '{kind}' is a bare-string kind; it has no enumerable sub-types"
            )
        return tuple(entry.keys())

    def role_of(self, kind: str, sub_type: str | None) -> str:
        """Resolve a kind (and its sub-type, when object-valued) to a warehouse role.

        Args:
            kind: A records-category kind name.
            sub_type: The row's sub-type discriminator value
                (`prop__<kind>_type`) when `kind` is object-valued; None for a
                bare-string kind. For a bare-string kind any value is ignored;
                for an object-valued kind a non-None declared sub-type is
                required.

        Returns:
            "dimension" or "fact".

        Raises:
            KeyError: `kind` is not in the registry, or `kind` is object-valued
                and `sub_type` is not one of its declared sub-types.
            ValueError: `kind` is object-valued and `sub_type` is None.
        """
        entry = self._registry[kind]  # raises KeyError when absent
        if isinstance(entry, Mapping):
            if sub_type is None:
                raise ValueError(
                    f"kind '{kind}' is object-valued; sub_type must not be None"
                )
            return entry[sub_type]  # raises KeyError when sub_type not declared
        return entry


def _parse_record_roles(raw: object) -> RecordRoles | None:
    """Parse the optional record_roles block from the sidecar.

    Args:
        raw: The raw record_roles value from the sidecar (may be absent/None).

    Returns:
        A RecordRoles if the record_roles key is present and is a dict, None
        when the key is absent or not a mapping. Lenient parse — structural
        diagnosis is C1's job.
    """
    if not isinstance(raw, dict):
        return None
    registry: dict[str, str | dict[str, str]] = {}
    for kind, value in raw.items():
        if isinstance(value, str):
            registry[kind] = value
        elif isinstance(value, dict):
            sub_map: dict[str, str] = {
                k: v for k, v in value.items() if isinstance(v, str)
            }
            registry[kind] = sub_map
    return RecordRoles(_registry=registry)


@dataclass(frozen=True)
class SubTypeColumns:
    """Typed view of the sidecar `sub_type_columns` partition.

    Wraps the per-sub-type declared-column partition from `base.json`: a nested
    `{kind: {sub_type: (column-name, ...)}}` naming, for each sub-typed records
    kind, the value columns (`prop__<name>`, plus `ref_index__<name>` for
    reference-typed properties) each declared sub-type owns. It is the
    NULL-disambiguation surface — a NULL in a column the row's sub-type does not
    own is *structurally inapplicable*, not merely unrecorded. Declared
    applicability (intent, not observation), like `enum_domains`; slice-stable.
    The kind-wide discriminator `prop__<kind>_type` belongs to no sub-type's
    list (contract carve-out). Built from the sidecar; never re-exported from a
    producer type.
    """

    _partition: Mapping[str, Mapping[str, tuple[str, ...]]]

    def kinds(self) -> tuple[str, ...]:
        """The sub-typed kinds carried by the partition.

        Returns:
            Kind names in sidecar order — which the contract guarantees is
            lexicographic. Trusts that order rather than re-sorting (faithful
            read).
        """
        return tuple(self._partition.keys())

    def sub_types(self, kind: str) -> tuple[str, ...]:
        """The declared sub-types of a kind.

        Args:
            kind: A sub-typed records-category kind name.

        Returns:
            Sub-type names in sidecar order — every declared sub-type, never
            narrowed to those with surviving rows (slice-stable).

        Raises:
            KeyError: `kind` is not in the partition.
        """
        return tuple(self._partition[kind].keys())

    def columns_for(self, kind: str, sub_type: str) -> tuple[str, ...]:
        """The value columns sub-type `sub_type` of `kind` declares.

        Args:
            kind: A sub-typed records-category kind name.
            sub_type: A declared sub-type of `kind`.

        Returns:
            Column names in the kind's union column order (`ref_index__<name>`
            immediately after its own `prop__<name>`). May be empty for a
            sub-type whose declared properties are all collection-struct — the
            key is kept, never dropped.

        Raises:
            KeyError: `kind` is not in the partition, or `sub_type` is not a
                declared sub-type of `kind`.
        """
        return self._partition[kind][sub_type]


def _parse_sub_type_columns(raw: object) -> SubTypeColumns | None:
    """Parse the optional sub_type_columns block from the sidecar.

    Args:
        raw: The raw sub_type_columns value from the sidecar (may be
            absent/None).

    Returns:
        A SubTypeColumns when the key is present and is a dict; None when the
        key is absent or not a mapping. Absence (None) is deliberately
        distinguishable from a present-but-empty per-sub-type list: a consumer
        falls back to union-schema behaviour on None, not on an empty list.
        Lenient parse — structural diagnosis is C1/C14's job.
    """
    if not isinstance(raw, dict):
        return None
    partition: dict[str, Mapping[str, tuple[str, ...]]] = {}
    for kind, sub_map in raw.items():
        if not isinstance(sub_map, dict):
            continue
        inner: dict[str, tuple[str, ...]] = {}
        for sub_type, cols in sub_map.items():
            if isinstance(cols, list):
                inner[sub_type] = tuple(c for c in cols if isinstance(c, str))
        partition[kind] = inner
    return SubTypeColumns(_partition=partition)


def _discriminator_domain(
    enum_domains: Mapping[str, Mapping[str, tuple[str, ...]]], kind: str
) -> tuple[str, ...]:
    """The declared `<kind>_type` discriminator domain for `kind`, or `()`.

    Shared by `Sidecar.subtype_values` and the `presentation_keys` builder so
    the two consult one lookup rule (`enum_domains[kind][f"{kind}_type"]`),
    never two.

    Args:
        enum_domains: The sidecar's parsed enum_domains registry.
        kind: A records-category kind name.

    Returns:
        The declared sub-type values, or `()` when `kind` carries no
        `<kind>_type` entry.
    """
    kind_domains = enum_domains.get(kind)
    if kind_domains is None:
        return ()
    return kind_domains.get(f"{kind}_type", ())


@dataclass(frozen=True)
class KeySpace:
    """A minting declaration's key-space identity, verbatim from the sidecar.

    `space_class` carries the sidecar field `class` (a Python keyword).
    `prefix` and `width` are present iff the class is digit-rendered
    ('counter' / 'record_index') — the contract's presence rule, mirrored as
    None-ness rather than sentinel values.
    """

    space_class: Literal["counter", "record_index", "uuid", "record_id"]
    prefix: str | None
    width: int | None


@dataclass(frozen=True)
class PartitionKey:
    """One minting declaration's key claims, scoped to its partition.

    All claims range over the partition's cells, which the contract declares
    total non-NULL (a declared partition has no NULL `presentation_id`).
    """

    unique_within: Literal["emit", "branch"]
    branch_stable: bool
    slice_stable: bool
    key_space: KeySpace


@dataclass(frozen=True)
class WholeColumnClaim:
    """A whole-column key claim: a kind rollup, or an algebra-derived union.

    unique_within is None when no uniqueness claim is derivable — "no
    claim", never "not unique".
    """

    unique_within: Literal["emit", "branch"] | None
    branch_stable: bool
    slice_stable: bool


@dataclass(frozen=True)
class _KindEntry:
    """One kind's parsed, coherent presentation_keys entry.

    Exactly one of (`key`) or (`sub_types`, `rollup`) is populated —
    `key` for a flat kind, the pair for a partitioned kind — mirroring the
    sidecar's own flat/partitioned grammar discriminator.
    """

    key: PartitionKey | None
    sub_types: Mapping[str, PartitionKey] | None
    rollup: WholeColumnClaim | None


@dataclass(frozen=True)
class PresentationKeys:
    """Typed view of the sidecar `presentation_keys` registry.

    Verbatim carry of the per-kind key-claim block: per minting declaration
    (per sub-type for partitioned kinds, a single entry for flat kinds), the
    key scalars and key-space identity, plus the kind rollup for partitioned
    kinds. Constructed only from a coherent block — `Sidecar.
    presentation_keys()` raises rather than yield an incoherent view. Built
    from the sidecar; never re-exported from a producer type.
    """

    _entries: Mapping[str, _KindEntry]

    def kinds(self) -> tuple[str, ...]:
        """The kinds carrying claims, in sidecar (lexicographic) order.

        Returns:
            Kind names, verbatim order.
        """
        return tuple(self._entries.keys())

    def is_partitioned(self, kind: str) -> bool:
        """Whether a kind's entry is per-sub-type (`sub_types`) or flat (`key`).

        Args:
            kind: A kind present in the block.

        Returns:
            True iff the kind's entry carries `sub_types`.

        Raises:
            KeyError: `kind` is not in the block.
        """
        return self._entries[kind].sub_types is not None

    def key(self, kind: str) -> PartitionKey:
        """A flat kind's single declaration — the whole-column claim.

        Args:
            kind: A kind present in the block.

        Returns:
            The `key` entry's claims.

        Raises:
            KeyError: `kind` is not in the block.
            ValueError: `kind` is partitioned (read `key_for` / rollup
                instead).
        """
        entry = self._entries[kind]
        if entry.key is None:
            raise ValueError(
                f"kind '{kind}' is partitioned; use key_for / whole_table_claim "
                "instead of key"
            )
        return entry.key

    def sub_types(self, kind: str) -> tuple[str, ...]:
        """A partitioned kind's declared (minting) sub-types, sidecar order.

        Every sub-type whose declaration mints, zero-row partitions included;
        never narrowed to sub-types with surviving rows.

        Args:
            kind: A kind present in the block.

        Returns:
            Sub-type names, verbatim order.

        Raises:
            KeyError: `kind` is not in the block.
            ValueError: `kind` is flat and has no enumerable sub-types.
        """
        entry = self._entries[kind]
        if entry.sub_types is None:
            raise ValueError(f"kind '{kind}' is flat; it has no enumerable sub-types")
        return tuple(entry.sub_types.keys())

    def key_for(self, kind: str, sub_type: str) -> PartitionKey:
        """A partitioned kind's per-sub-type declaration.

        Presence is itself a claim: every row of this sub-type carries a
        non-NULL `presentation_id`.

        Args:
            kind: A kind present in the block.
            sub_type: A declared sub-type of `kind`.

        Returns:
            That sub-type's claims.

        Raises:
            KeyError: `kind` is not in the block, or `sub_type` is not among
                its declared entries (an undeclared sub-type mints nothing —
                its cells are NULL, and it carries no claims).
            ValueError: `kind` is flat.
        """
        entry = self._entries[kind]
        if entry.sub_types is None:
            raise ValueError(f"kind '{kind}' is flat; use key instead of key_for")
        return entry.sub_types[sub_type]

    def whole_table_claim(self, kind: str) -> WholeColumnClaim:
        """The whole-column claim for a kind, whatever its entry shape.

        The one method a consumer keying a whole-kind table reads: a flat
        kind's `key` scalars, a partitioned kind's rollup.

        Args:
            kind: A kind present in the block.

        Returns:
            The whole-column claim; `unique_within` None when the rollup
            derives no claim.

        Raises:
            KeyError: `kind` is not in the block.
        """
        entry = self._entries[kind]
        if entry.key is not None:
            return WholeColumnClaim(
                unique_within=entry.key.unique_within,
                branch_stable=entry.key.branch_stable,
                slice_stable=entry.key.slice_stable,
            )
        assert entry.rollup is not None  # a partitioned entry always carries a rollup
        return entry.rollup


_KEY_SPACE_CLASSES: frozenset[str] = frozenset(
    {"counter", "record_index", "uuid", "record_id"}
)
_DIGIT_RENDERED_CLASSES: frozenset[str] = frozenset({"counter", "record_index"})

#: Scalars `key_space.class` determines: (unique_within, branch_stable, slice_stable).
_EXPECTED_SCALARS: Mapping[str, tuple[Literal["emit", "branch"], bool, bool]] = {
    "counter": ("emit", False, False),
    "record_index": ("branch", True, True),
    "uuid": ("branch", True, True),
    "record_id": ("branch", True, True),
}


def _digit_suffix_extends(shorter: str, longer: str) -> bool:
    """Whether `longer` equals `shorter` plus a possibly-empty digit string."""
    if not longer.startswith(shorter):
        return False
    remainder = longer[len(shorter) :]
    return remainder == "" or remainder.isdigit()


def _prefixes_comparable(prefix_a: str, prefix_b: str) -> bool:
    """Whether two digit-rendered prefixes are comparable (contract § algebra).

    Comparable iff one equals the other plus a possibly-empty digit string —
    so equal prefixes are comparable. Comparable prefixes make the pair
    union-unsafe.
    """
    if len(prefix_a) <= len(prefix_b):
        return _digit_suffix_extends(prefix_a, prefix_b)
    return _digit_suffix_extends(prefix_b, prefix_a)


def union_safe(
    a: KeySpace,
    b: KeySpace,
) -> bool:
    """Whether two key spaces of one kind are union-safe.

    The contract's normative pairwise algebra: a value collision must be
    impossible given only the declarations. Kind-scoped — callers must not
    pass entries of different kinds (the spaces make no cross-kind claim,
    and the function cannot detect the misuse).

    Args:
        a: One declaration's key space.
        b: Another declaration's key space, same kind.

    Returns:
        True iff the pair is union-safe per the contract's table (shared
        injective `record_index` space; independent `uuid` draws; verbatim
        `record_id`; digit-rendered pairs with incomparable prefixes).
    """
    if (
        a.space_class == "record_index"
        and b.space_class == "record_index"
        and a.prefix == b.prefix
        and a.width == b.width
    ):
        return True
    if a.space_class == "uuid" and b.space_class == "uuid":
        return True
    if a.space_class == "record_id" and b.space_class == "record_id":
        return True
    if (
        a.space_class in _DIGIT_RENDERED_CLASSES
        and b.space_class in _DIGIT_RENDERED_CLASSES
    ):
        assert a.prefix is not None and b.prefix is not None
        return not _prefixes_comparable(a.prefix, b.prefix)
    return False


def combined_claim(
    entries: Sequence[PartitionKey],
) -> WholeColumnClaim:
    """The whole-column claim for a union of one kind's partitions.

    The contract's combined-set derivation: pairwise-unsafe sets carry no
    uniqueness claim; otherwise all-counter → 'emit', all-stable →
    'branch', mixed → 'branch'; the stability pair is true/true iff every
    member is stable-class. A singleton set's claim equals its entry's
    scalars.

    Args:
        entries: One kind's declarations (any subset, one or more).

    Returns:
        The union's claim.

    Raises:
        ValueError: `entries` is empty — an empty union has no claim to
            state and a caller reaching it holds a logic error.
    """
    if not entries:
        raise ValueError("combined_claim requires at least one entry")
    if len(entries) == 1:
        only = entries[0]
        return WholeColumnClaim(
            unique_within=only.unique_within,
            branch_stable=only.branch_stable,
            slice_stable=only.slice_stable,
        )
    all_stable = all(entry.branch_stable for entry in entries)
    all_counter = all(not entry.branch_stable for entry in entries)
    pairwise_safe = all(
        union_safe(x.key_space, y.key_space)
        for x, y in itertools.combinations(entries, 2)
    )
    if not pairwise_safe:
        return WholeColumnClaim(
            unique_within=None, branch_stable=all_stable, slice_stable=all_stable
        )
    if all_counter:
        return WholeColumnClaim(
            unique_within="emit", branch_stable=False, slice_stable=False
        )
    if all_stable:
        return WholeColumnClaim(
            unique_within="branch", branch_stable=True, slice_stable=True
        )
    return WholeColumnClaim(
        unique_within="branch", branch_stable=False, slice_stable=False
    )


def _parse_key_space(raw: object, context: str) -> KeySpace:
    """Parse and validate one `key_space` object (clause f: key-space shape).

    Args:
        raw: The raw `key_space` value.
        context: A description of the enclosing entry, for error messages.

    Returns:
        The parsed KeySpace.

    Raises:
        PresentationKeysInvalidError: `raw` is not an object, `class` is
            outside the four-member enum, or `prefix`/`width` presence
            disagrees with whether `class` is digit-rendered.
    """
    if not isinstance(raw, Mapping):
        raise PresentationKeysInvalidError(
            f"{context}: key_space is missing or not an object"
        )
    space_class_raw = raw.get("class")
    if space_class_raw not in _KEY_SPACE_CLASSES:
        raise PresentationKeysInvalidError(
            f"{context}: key_space.class {space_class_raw!r} is not one of "
            f"{sorted(_KEY_SPACE_CLASSES)} (clause: key-space shape)"
        )
    space_class = cast(
        Literal["counter", "record_index", "uuid", "record_id"], space_class_raw
    )
    digit_rendered = space_class in _DIGIT_RENDERED_CLASSES
    prefix_present = "prefix" in raw
    width_present = "width" in raw
    if digit_rendered != prefix_present or digit_rendered != width_present:
        raise PresentationKeysInvalidError(
            f"{context}: key_space.class {space_class!r} prefix/width presence "
            f"must match digit-rendered={digit_rendered} (clause: key-space shape)"
        )
    if not digit_rendered:
        return KeySpace(space_class=space_class, prefix=None, width=None)
    prefix_raw = raw.get("prefix")
    width_raw = raw.get("width")
    if (
        not isinstance(prefix_raw, str)
        or not isinstance(width_raw, int)
        or isinstance(width_raw, bool)
    ):
        raise PresentationKeysInvalidError(
            f"{context}: key_space.prefix/width must be a string/integer "
            "(clause: key-space shape)"
        )
    return KeySpace(space_class=space_class, prefix=prefix_raw, width=width_raw)


def _parse_partition_key(raw: object, context: str) -> PartitionKey:
    """Parse and validate one `partition_key` object.

    Args:
        raw: The raw partition-key value (a `key` entry or one `sub_types`
            member).
        context: A description of the enclosing entry, for error messages.

    Returns:
        The parsed PartitionKey.

    Raises:
        PresentationKeysInvalidError: `raw` is not an object, its scalars are
            missing/mistyped, its key_space is invalid (clause f), or its
            scalars disagree with `key_space.class` (clause e).
    """
    if not isinstance(raw, Mapping):
        raise PresentationKeysInvalidError(f"{context}: entry is not an object")
    key_space = _parse_key_space(raw.get("key_space"), context)
    unique_within_raw = raw.get("unique_within")
    branch_stable_raw = raw.get("branch_stable")
    slice_stable_raw = raw.get("slice_stable")
    if (
        unique_within_raw not in ("emit", "branch")
        or not isinstance(branch_stable_raw, bool)
        or not isinstance(slice_stable_raw, bool)
    ):
        raise PresentationKeysInvalidError(
            f"{context}: unique_within/branch_stable/slice_stable missing or mistyped"
        )
    actual = (unique_within_raw, branch_stable_raw, slice_stable_raw)
    expected = _EXPECTED_SCALARS[key_space.space_class]
    if actual != expected:
        raise PresentationKeysInvalidError(
            f"{context}: scalars {actual} inconsistent with key_space.class "
            f"{key_space.space_class!r} (expected {expected}) "
            "(clause: scalar-key-space coupling)"
        )
    return PartitionKey(
        unique_within=cast(Literal["emit", "branch"], unique_within_raw),
        branch_stable=branch_stable_raw,
        slice_stable=slice_stable_raw,
        key_space=key_space,
    )


def _parse_rollup_claim(
    raw_entry: Mapping[str, object], context: str
) -> WholeColumnClaim:
    """Parse a partitioned kind's whole-column rollup.

    Args:
        raw_entry: The kind's raw block entry (carries `unique_within`
            optionally, `branch_stable`/`slice_stable` required, alongside
            `sub_types`).
        context: A description of the kind, for error messages.

    Returns:
        The parsed rollup claim.

    Raises:
        PresentationKeysInvalidError: `unique_within` is present but outside
            the two-member enum, or `branch_stable`/`slice_stable` is
            missing or mistyped.
    """
    unique_within_present = "unique_within" in raw_entry
    unique_within_raw = raw_entry.get("unique_within")
    if unique_within_present and unique_within_raw not in ("emit", "branch"):
        raise PresentationKeysInvalidError(
            f"{context}: rollup unique_within {unique_within_raw!r} is invalid"
        )
    branch_stable_raw = raw_entry.get("branch_stable")
    slice_stable_raw = raw_entry.get("slice_stable")
    if not isinstance(branch_stable_raw, bool) or not isinstance(
        slice_stable_raw, bool
    ):
        raise PresentationKeysInvalidError(
            f"{context}: rollup branch_stable/slice_stable missing or mistyped"
        )
    unique_within = (
        cast(Literal["emit", "branch"], unique_within_raw)
        if unique_within_present
        else None
    )
    return WholeColumnClaim(
        unique_within=unique_within,
        branch_stable=branch_stable_raw,
        slice_stable=slice_stable_raw,
    )


def _parse_kind_entry(
    kind: str,
    raw_entry: object,
    discriminator_domain: tuple[str, ...],
) -> _KindEntry:
    """Parse and validate one kind's presentation_keys entry.

    Args:
        kind: The kind name, for error messages.
        raw_entry: The raw block entry for `kind`.
        discriminator_domain: `kind`'s declared `<kind>_type` sub-type
            domain; empty iff `kind` is flat.

    Returns:
        The parsed, coherent kind entry.

    Raises:
        PresentationKeysInvalidError: The entry is not an object, its shape
            disagrees with the discriminator domain (clause c), a
            `sub_types` key is outside the domain (clause d), a partition
            key is malformed (clauses e/f), or the rollup disagrees with
            `combined_claim` (clause g).
    """
    if not isinstance(raw_entry, Mapping):
        raise PresentationKeysInvalidError(f"kind '{kind}': entry is not an object")
    is_discriminated = bool(discriminator_domain)
    has_key = "key" in raw_entry
    has_sub_types = "sub_types" in raw_entry

    if is_discriminated:
        if has_key or not has_sub_types:
            raise PresentationKeysInvalidError(
                f"kind '{kind}': discriminator-bearing kind must carry a "
                "sub_types entry, not key (clause: entry shape)"
            )
        sub_types_raw = raw_entry["sub_types"]
        if not isinstance(sub_types_raw, Mapping) or not sub_types_raw:
            raise PresentationKeysInvalidError(
                f"kind '{kind}': sub_types must be a non-empty object"
            )
        domain = frozenset(discriminator_domain)
        sub_entries: dict[str, PartitionKey] = {}
        for sub_type, sub_raw in sub_types_raw.items():
            if sub_type not in domain:
                raise PresentationKeysInvalidError(
                    f"kind '{kind}' sub_type '{sub_type}': not in the "
                    "discriminator domain (clause: sub-type domain)"
                )
            sub_entries[sub_type] = _parse_partition_key(
                sub_raw, f"kind '{kind}' sub_type '{sub_type}'"
            )
        rollup = _parse_rollup_claim(raw_entry, f"kind '{kind}'")
        expected_rollup = combined_claim(tuple(sub_entries.values()))
        if rollup != expected_rollup:
            raise PresentationKeysInvalidError(
                f"kind '{kind}': rollup {rollup} disagrees with the union "
                f"algebra's {expected_rollup} (clause: rollup consistency)"
            )
        return _KindEntry(key=None, sub_types=sub_entries, rollup=rollup)

    if has_sub_types or not has_key:
        raise PresentationKeysInvalidError(
            f"kind '{kind}': flat kind must carry a key entry, not sub_types "
            "(clause: entry shape)"
        )
    key = _parse_partition_key(raw_entry["key"], f"kind '{kind}'")
    return _KindEntry(key=key, sub_types=None, rollup=None)


def _presentation_id_kinds(tables: tuple[TableSpec, ...]) -> frozenset[str]:
    """The kinds whose `records__<kind>` table carries a `presentation_id` column."""
    return frozenset(
        table.record_kind
        for table in tables
        if table.category == "records"
        and table.record_kind is not None
        and any(col.name == "presentation_id" for col in table.columns)
    )


def _build_presentation_keys(
    raw_block: Mapping[str, object],
    tables: tuple[TableSpec, ...],
    enum_domains: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> PresentationKeys:
    """Strict-parse the sidecar `presentation_keys` block into a typed view.

    Args:
        raw_block: The raw `presentation_keys` mapping.
        tables: The sidecar's parsed tables, for the membership clause.
        enum_domains: The sidecar's parsed enum_domains registry, for the
            entry-shape and sub-type-domain clauses.

    Returns:
        The coherent typed view.

    Raises:
        PresentationKeysInvalidError: Any of the six coherence clauses is
            violated, naming the kind (and sub-type) and the clause.
    """
    presentation_id_kinds = _presentation_id_kinds(tables)
    block_kinds = frozenset(raw_block.keys())

    extra_in_block = sorted(block_kinds - presentation_id_kinds)
    if extra_in_block:
        kind = extra_in_block[0]
        raise PresentationKeysInvalidError(
            f"kind '{kind}': presentation_keys entry present but "
            f"records__{kind} carries no presentation_id column "
            "(clause: kind membership)"
        )
    missing_from_block = sorted(presentation_id_kinds - block_kinds)
    if missing_from_block:
        kind = missing_from_block[0]
        raise PresentationKeysInvalidError(
            f"kind '{kind}': records__{kind} carries a presentation_id column "
            "but no presentation_keys entry (clause: kind membership)"
        )

    entries: dict[str, _KindEntry] = {}
    for kind, raw_entry in raw_block.items():
        domain = _discriminator_domain(enum_domains, kind)
        entries[kind] = _parse_kind_entry(kind, raw_entry, domain)
    return PresentationKeys(_entries=entries)


class Sidecar:
    """Typed, read-only view of a base-layer emit's base.json.

    Every table/column/branch question is answered from here, never from the spec.
    """

    def __init__(
        self,
        raw: Mapping[str, object],
        base_format_version: int,
        branches: tuple[BranchEntry, ...],
        tables: tuple[TableSpec, ...],
        runtime: RuntimeAnchor | None,
        pinned_ids: Mapping[str, Mapping[str, str]],
        enum_domains: Mapping[str, Mapping[str, tuple[str, ...]]],
        record_roles: RecordRoles | None,
        sub_type_columns: SubTypeColumns | None,
        presentation_keys_raw: Mapping[str, object] | None,
        row_census: BranchCensus | None,
    ) -> None:
        self._raw = raw
        self._base_format_version = base_format_version
        self._branches = branches
        self._tables = tables
        self._runtime = runtime
        self._pinned_ids = pinned_ids
        self._enum_domains = enum_domains
        self._record_roles = record_roles
        self._sub_type_columns = sub_type_columns
        self._presentation_keys_raw = presentation_keys_raw
        self._row_census = row_census

    @classmethod
    def from_raw(cls, raw: Mapping[str, object]) -> "Sidecar":
        """Version-gate and structurally parse a parsed base.json mapping.

        Performs, in order: first, the version gate — base_format_version must be a
        present int (strict: isinstance(v, int) and not isinstance(v, bool)) equal to
        the imported SUPPORTED_BASE_FORMAT_VERSION. Second, the structural floor — the
        required, non-defaulted descriptor fields
        (TableSpec.{name,category,columns,rows}, ColumnSpec.{name,type},
        BranchEntry.{fork_path,parent,slice_at}) must be present and correctly typed.
        Gating precedes structural parse so a future unsupported-version sidecar
        fails with a clear version error, never an opaque structural one.

        Does NOT enforce schema patterns / enums / const / minItems /
        conditional-required — those are C1's job. record_kind and property are
        populated by presence alone (absent -> None); the
        category<->record_kind/property correspondence is left to C1/C3.
        A present-but-schema-invalid sidecar (empty columns array, a phantom
        column) parses successfully here and is diagnosed later by validate —
        this is the room the negative fixtures need. A table `category`
        outside the contract's closed set ("fixed", "records", "membership")
        is the one exception: it refuses at this floor with
        SidecarStructureError, not deferred to validate — see Raises.

        Args:
            raw: The parsed base.json object exactly as loaded from disk.

        Returns:
            A Sidecar exposing typed branches / tables / columns / runtime /
            pinned_ids / enum_domains and the raw mapping (Sidecar.raw, authoritative
            input for C1).

        Raises:
            UnsupportedBaseFormatVersionError: base_format_version is a present int
                other than SUPPORTED_BASE_FORMAT_VERSION; carries it as found_version.
                No auto-upgrade.
            SidecarStructureError: base_format_version absent or not a strict int (a
                JSON float like 3.0, a bool, a string, or null all route here), OR a
                required top-level / structural-floor field is absent or mis-typed
                (branches not a non-empty list, tables not a list, a table missing
                name/category/columns/rows, a columns element that is not an object or
                missing name/type, a branch missing fork_path/parent/slice_at), OR a
                table's `category` is a string outside the contract's closed set
                ("fixed", "records", "membership") — the same failure class as a
                missing or non-string category.
                `parent` must be PRESENT with a value that may be null (-> None); an
                absent parent key is below the floor.
        """
        version_raw = raw.get("base_format_version")

        if (
            version_raw is None
            or not isinstance(version_raw, int)
            or isinstance(version_raw, bool)
        ):
            raise SidecarStructureError(
                f"base_format_version must be a non-null integer; got {version_raw!r}"
            )

        if version_raw != SUPPORTED_BASE_FORMAT_VERSION:
            raise UnsupportedBaseFormatVersionError(found_version=version_raw)

        branches_raw = raw.get("branches")
        if not isinstance(branches_raw, list) or len(branches_raw) == 0:
            raise SidecarStructureError("branches must be a non-empty list")

        tables_raw = raw.get("tables")
        if not isinstance(tables_raw, list):
            raise SidecarStructureError("tables must be a list")

        branches = tuple(_parse_branch(b, idx) for idx, b in enumerate(branches_raw))
        tables = tuple(_parse_table(t, idx) for idx, t in enumerate(tables_raw))

        runtime = _parse_runtime(raw.get("runtime"))
        pinned_ids = _parse_pinned_ids(raw.get("pinned_ids"))
        enum_domains = _parse_enum_domains(raw.get("enum_domains"))
        record_roles = _parse_record_roles(raw.get("record_roles"))
        sub_type_columns = _parse_sub_type_columns(raw.get("sub_type_columns"))
        presentation_keys_raw_untyped = raw.get("presentation_keys")
        presentation_keys_raw: Mapping[str, object] | None = (
            presentation_keys_raw_untyped
            if isinstance(presentation_keys_raw_untyped, dict)
            else None
        )

        return cls(
            raw=raw,
            base_format_version=version_raw,
            branches=branches,
            tables=tables,
            runtime=runtime,
            pinned_ids=pinned_ids,
            enum_domains=enum_domains,
            record_roles=record_roles,
            sub_type_columns=sub_type_columns,
            presentation_keys_raw=presentation_keys_raw,
            row_census=_parse_row_census(raw.get("row_census"), branches[0].fork_path),
        )

    @property
    def base_format_version(self) -> int:
        """The gated format version (always SUPPORTED_BASE_FORMAT_VERSION once open)."""
        return self._base_format_version

    @property
    def row_census(self) -> BranchCensus | None:
        """Row counts for the emit's single branch, or None when the emit carries none.

        The block is optional and advisory — no conformance check ranges over it —
        so a consumer reads it as evidence and must have a defined path for None.
        """
        return self._row_census

    @property
    def raw(self) -> Mapping[str, object]:
        """The parsed base.json exactly as on disk. Authoritative input for C1."""
        return self._raw

    def branches(self) -> tuple[BranchEntry, ...]:
        """All branches, in the order the sidecar lists them.

        The producer writes them in branch tuple-lexicographic order; the reader
        preserves that order and does not re-sort.
        """
        return self._branches

    def runtime(self) -> RuntimeAnchor | None:
        """The wallclock anchor, or None when the scenario declared no runtime block."""
        return self._runtime

    def tables(self) -> tuple[TableSpec, ...]:
        """All tables, in DuckDB-catalog order."""
        return self._tables

    def table(self, name: str) -> TableSpec:
        """The table named `name`.

        Args:
            name: DuckDB table name.

        Returns:
            The matching TableSpec.

        Raises:
            TableNotFoundError: No table named `name` is declared in the sidecar.

        Note:
            A conformant sidecar never declares two tables with the same name
            (DuckDB cannot hold duplicates). If a non-conformant one does, the first
            match in sidecar order is returned, keeping the accessor deterministic;
            `columns(name)` resolves to that same first match.
        """
        for t in self._tables:
            if t.name == name:
                return t
        raise TableNotFoundError(f"no table named '{name}' in sidecar")

    def columns(self, table_name: str) -> tuple[ColumnSpec, ...]:
        """Columns of `table_name` in catalog order.

        Args:
            table_name: DuckDB table name.

        Returns:
            The table's columns in declared order.

        Raises:
            TableNotFoundError: No table named `table_name` is declared.
        """
        return self.table(table_name).columns

    def _column(self, table_name: str, column_name: str) -> ColumnSpec:
        """The ColumnSpec named `column_name` on `table_name`.

        Args:
            table_name: DuckDB table name.
            column_name: Column name, including its prop__ prefix.

        Returns:
            The matching ColumnSpec.

        Raises:
            TableNotFoundError: No table named `table_name` is declared.
            ColumnNotFoundError: The table declares no column named `column_name`.
        """
        for col in self.columns(table_name):
            if col.name == column_name:
                return col
        raise ColumnNotFoundError(
            f"no column named '{column_name}' on table '{table_name}'"
        )

    def temporal_class(self, table_name: str, column_name: str) -> TemporalClass:
        """The declared point-in-time class of one value-carrying column.

        The single point where the sidecar's verbatim declared value narrows to a
        TemporalClass; every surface that needs a class (the genre predicate) resolves
        through it.

        Args:
            table_name: DuckDB table name.
            column_name: Column name, including its prop__ prefix.

        Returns:
            The column's declared TemporalClass.

        Raises:
            TableNotFoundError: No table named `table_name` is declared.
            ColumnNotFoundError: The table declares no column named `column_name`.
            TemporalClassUnavailableError: The column has no usable class. Three cases,
                distinguished in the message: the column carries neither temporal
                attribute (a structural, identity, or membership column — conformant;
                it has no temporal semantics to ask about); it declares
                history_tracked but no temporal_class (non-conformant, C13); or it
                declares a value outside the three-class enum (non-conformant — C13's
                enum clause, and C1, since the vendored schema enum-constrains the
                value). The non-conformant messages direct the caller to
                `fabulexa-forge validate`. No class is ever inferred.
        """
        col = self._column(table_name, column_name)
        qualified = f"{table_name}.{column_name}"
        if col.history_tracked is None and col.temporal_class is None:
            raise TemporalClassUnavailableError(
                f"{qualified} carries no temporal attributes; it has no "
                "point-in-time class to ask about"
            )
        if col.temporal_class is None:
            raise TemporalClassUnavailableError(
                f"{qualified} declares history_tracked but no temporal_class; "
                "the emit is non-conformant (C13). Run `fabulexa-forge validate`."
            )
        if col.temporal_class not in _TEMPORAL_CLASSES:
            raise TemporalClassUnavailableError(
                f"{qualified} declares temporal_class {col.temporal_class!r}, "
                "outside the constant/tracked/slice_only enum; the emit is "
                "non-conformant (C13). Run `fabulexa-forge validate`."
            )
        return cast(TemporalClass, col.temporal_class)

    def history_tracked_available(self) -> bool:
        """Whether this emit carries the per-column history_tracked flag.

        Returns:
            True iff at least one column declares history_tracked (the flag is
            all-or-none per emit, so presence on any column implies presence on
            all). False for an emit that predates the flag; consumers then fall
            back to history-table inference.
        """
        return any(
            col.history_tracked is not None
            for table in self._tables
            for col in table.columns
        )

    def pinned_ids(self) -> Mapping[str, Mapping[str, str]]:
        """The pin surface {kind: {label: id}}; an empty mapping when absent."""
        return self._pinned_ids

    def enum_domains(self) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
        """Closed-domain registry {kind: {property: (option, ...)}}; empty if absent."""
        return self._enum_domains

    def record_roles(self) -> RecordRoles | None:
        """The typed record-role registry, or None when the sidecar omits it.

        Read-only role metadata overlaid on the already-discovered records tables;
        it does not participate in table/column discovery.

        Returns:
            A RecordRoles view when `base.json` carries `record_roles`; None when
            the field is absent (an emit predating the registry). Absence is "role
            unknown", not an error — C12 skips on absence.
        """
        return self._record_roles

    def sub_type_columns(self) -> SubTypeColumns | None:
        """The typed sub-type column partition, or None when the sidecar omits it.

        Read-only declared-applicability metadata overlaid on the already-
        discovered records tables; it does not participate in table/column
        discovery. Absence (an emit predating the field, or a run carrying no
        partition) is "partition unknown", not an error — a consumer falls back
        to union-schema behaviour, treating every union column as applicable.

        Returns:
            A SubTypeColumns view when `base.json` carries `sub_type_columns`;
            None when the field is absent.
        """
        return self._sub_type_columns

    def subtype_values(self, kind: str) -> tuple[str, ...]:
        """The declared `<kind>_type` sub-type discriminator values for a kind.

        The single, reader-first oracle for "is this kind sub-typed, and into which
        sub-types does it split?" — sourced from the sidecar's closed-domain
        registry at ``enum_domains[kind]["<kind>_type"]``, the contract's
        authoritative declared key set for a sub-typed kind. Owns the ``<kind>_type``
        naming convention and the intent-not-observation rule: a declared sub-type is
        returned even when the slice materialises zero rows for it.

        A kind is sub-typed (splits into one topic per sub-type) iff this returns a
        non-empty tuple. Independent of ``record_roles[kind]``'s shape — a kind whose
        warehouse role is a bare string (e.g. ``entity`` -> "dimension") is still
        sub-typed when it carries a ``<kind>_type`` domain.

        Args:
            kind: A records-category kind name.

        Returns:
            The declared sub-type values in ``enum_domains`` declaration order, or
            ``()`` when the kind carries no ``<kind>_type`` discriminator domain (it
            is not sub-typed and routes to a single topic).

            Total by design — it never raises, returning ``()`` for three distinct
            cases: a kind with no ``<kind>_type`` entry, an absent ``enum_domains``,
            and an unknown kind. The first two are genuine "not sub-typed" verdicts.
            The unknown-kind case is a total-function convenience only: on the
            streaming path an unknown kind is rejected upstream by
            ``StreamKindResolvable`` (``records__<kind>`` must resolve) before this
            accessor is consulted, so the unknown-kind diagnostic comes from that
            rule — never a misleading "not sub-typed" from a silent ``()`` here.
            Totality (vs. the ``KeyError``-raising ``RecordRoles`` accessors) is
            deliberate: routing asks this for every selected kind, and most kinds are
            legitimately not sub-typed.
        """
        return _discriminator_domain(self._enum_domains, kind)

    def presentation_keys(self) -> PresentationKeys | None:
        """The sidecar `presentation_keys` registry as a typed view.

        Method on `Sidecar`, sibling of `record_roles()` / `sub_type_columns()`.
        Verbatim carry; nothing inferred. Unlike its siblings the parse is
        strict: no conformance check owns this block's semantic rules, and a
        mended block would feed wrong keys to consumers, so an incoherent
        present block refuses rather than degrades.

        Returns:
            The typed view, or None when the sidecar carries no
            `presentation_keys` key ("no claims").

        Raises:
            PresentationKeysInvalidError: The block is present and violates a
                consistency clause (kind membership vs `presentation_id` column
                presence, entry shape vs discriminator domain, sub-type keys
                outside the domain, scalars inconsistent with `key_space.class`,
                key-space presence-rule violation, or rollup inconsistent with
                the union algebra) — the message names the kind, sub-type, and
                clause.
        """
        if self._presentation_keys_raw is None:
            return None
        return _build_presentation_keys(
            self._presentation_keys_raw, self._tables, self._enum_domains
        )
