"""The records-column taxonomy: the one classifier every records-column
consumer reads through.

Pure name classification over a records-category column name — no sidecar, no
DuckDB, no context. `records_column_role` is total over the four contract
column families (`design doc § Semantics — the records-column taxonomy`);
every other name is a no-role condition every caller must treat as loud
(conformance records a C5 failure; the source exporter raises
`SourceUnclassifiedColumn`) — never a silent fall-through.

Also owns the structural-temporal surface (`structural_instant_columns`,
`records_structural_column_is_mutable`): which structural columns of a table
category carry a sim-time instant, and which records structural columns may
change after creation. Both are pure and emit-independent — contract facts,
not presentation — and loud on anything outside their closed domains (design
doc § Semantics — Loudness).
"""

from __future__ import annotations

from typing import Final, Literal, Mapping

RecordsColumnRole = Literal["identity", "presentation", "lifecycle", "payload"]

#: Prefix marking a records-table reference-identity sibling column.
REF_INDEX_PREFIX: Final[str] = "ref_index__"

#: Prefix marking a records-table scalar-property (payload) column.
_PROP_PREFIX: Final[str] = "prop__"

#: Bare identity column names (`ref_index__<name>` is matched by prefix, not
#: enumerated here).
_IDENTITY_NAMES: Final[frozenset[str]] = frozenset(
    {"fork_path", "record_id", "record_index"}
)

#: The sole presentation column name.
_PRESENTATION_NAMES: Final[frozenset[str]] = frozenset({"presentation_id"})

#: Lifecycle column names.
_LIFECYCLE_NAMES: Final[frozenset[str]] = frozenset(
    {"created_sim_time", "active", "deactivated_at", "last_mutation_sim_time"}
)


def records_column_role(name: str) -> RecordsColumnRole | None:
    """
    Classify a records-category column name into its contract role.

    Pure and context-free: classification is by name family alone (design doc
    § Semantics — the records-column taxonomy). `None` means the name matches no
    records-category column family and is a loud condition at every call
    site — conformance records a C5 failure; an exporter raises. Callers MUST
    NOT treat `None` as "skip".

    Args:
        name: The column name as declared in the sidecar (or observed in the
            catalog) for a records-category table.

    Returns:
        The column's role, or None when the name matches no records-category
        column family.
    """
    if name in _IDENTITY_NAMES:
        return "identity"
    if name in _PRESENTATION_NAMES:
        return "presentation"
    if name in _LIFECYCLE_NAMES:
        return "lifecycle"
    if name.startswith(REF_INDEX_PREFIX) and len(name) > len(REF_INDEX_PREFIX):
        return "identity"
    if name.startswith(_PROP_PREFIX) and len(name) > len(_PROP_PREFIX):
        return "payload"
    return None


def ref_index_sibling(prop_column_name: str) -> str:
    """
    The `ref_index__<name>` column name paired with `prop__<name>`.

    The pairing is a pure name rule; whether the sibling is *required* on a
    given table is determined by the `prop__` column's sidecar `references`
    field, not by this function.

    Args:
        prop_column_name: A `prop__`-prefixed records payload column name.

    Returns:
        The sibling identity column name (`ref_index__` + the property name).

    Raises:
        ValueError: `prop_column_name` is not `prop__`-prefixed.
    """
    if not prop_column_name.startswith(_PROP_PREFIX):
        raise ValueError(
            f"'{prop_column_name}' is not prop__-prefixed; ref_index_sibling"
            " requires a records payload column name"
        )
    return REF_INDEX_PREFIX + prop_column_name[len(_PROP_PREFIX) :]


StructuralInstant = Literal[
    "created", "closed", "last_touched", "changed", "joined", "left"
]
"""The closed six-member instant vocabulary a structural column may name.

Presentation-free: no output name appears in it. The vocabulary derives from
the contract's column definitions (design doc § Semantics — the instant
vocabulary), not from any particular emit.
"""

#: The structural columns of each table category that carry a sim-time
#: instant, and the instant each names (design doc § Semantics — the instant
#: vocabulary). Contract-pinned, the same hardcoding class as the pinned
#: column lists — restates the vendored schema, never derived from an emit.
_STRUCTURAL_INSTANT_COLUMNS_BY_CATEGORY: Final[
    Mapping[str, Mapping[str, StructuralInstant]]
] = {
    "records": {
        "created_sim_time": "created",
        "deactivated_at": "closed",
        "last_mutation_sim_time": "last_touched",
    },
    "fixed": {
        "sim_time": "changed",
    },
    "membership": {
        "joined_sim_time": "joined",
        "left_sim_time": "left",
    },
}

#: Records structural columns whose value the producer may change after
#: creation (design doc § Semantics — Mutability).
_MUTABLE_RECORDS_STRUCTURAL_NAMES: Final[frozenset[str]] = frozenset(
    {"active", "deactivated_at", "last_mutation_sim_time"}
)

#: Records structural columns the contract pins as set once at creation.
_SET_ONCE_RECORDS_STRUCTURAL_NAMES: Final[frozenset[str]] = frozenset(
    {"created_sim_time", "fork_path", "record_id", "record_index", "presentation_id"}
)


def structural_instant_columns(category: str) -> Mapping[str, StructuralInstant]:
    """
    The structural columns of a table category that carry a sim-time instant.

    Pure and emit-independent: the mapping is a property of the contract's
    pinned column layout for the category, not of any particular emit. A
    category's mapping is the same for every emit at the supported format
    version. Columns absent from the returned mapping carry no instant.

    Args:
        category: The sidecar table category — "fixed", "records", or
            "membership".

    Returns:
        Column name to the instant it names, for every instant-carrying
        structural column of the category. Empty for a category that pins
        none.

    Raises:
        ValueError: `category` is not a recognised table category. The
            category set is closed and validated when the sidecar is read, so
            an unrecognised value is a caller error, never emit data.
    """
    try:
        return _STRUCTURAL_INSTANT_COLUMNS_BY_CATEGORY[category]
    except KeyError:
        raise ValueError(f"'{category}' is not a recognised table category") from None


def records_structural_column_is_mutable(name: str) -> bool:
    """
    Whether a records-table structural column's value may change after the
    record is created.

    Answers the structural half of temporal mutability only, over a closed
    domain: the contract's pinned records structural columns. A
    `prop__<name>` column's mutability is declared per-emit by its sidecar
    temporal pair and is not answered here; a `ref_index__<name>` column
    tracks its sibling `prop__<name>` and follows the sibling's answer. A
    caller needing both halves classifies through the records-column
    taxonomy first — routing by family plus the taxonomy's ref-index
    prefix rule, since `ref_index__<name>` classifies as `identity` — and
    asks each half in turn.

    Args:
        name: A records structural column name — one the records-column
            taxonomy classifies as `identity` (excluding the ref-index
            prefix), `presentation`, or `lifecycle`.

    Returns:
        True when the column is one whose value the producer may change
        after creation; False when the contract pins it as set once.

    Raises:
        ValueError: `name` is not a records structural column — a
            `prop__<name>`, a `ref_index__<name>`, or a name the contract
            does not pin. The structural set is closed; mutability of the
            open remainder is either the sidecar's question (`prop__`,
            `ref_index__`) or nowhere guaranteed (a producer-added column),
            so a silent False would state a fact the contract does not
            hold.
    """
    if name in _MUTABLE_RECORDS_STRUCTURAL_NAMES:
        return True
    if name in _SET_ONCE_RECORDS_STRUCTURAL_NAMES:
        return False
    raise ValueError(f"'{name}' is not a records structural column")
