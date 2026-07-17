"""The records-column taxonomy: the one classifier every records-column
consumer reads through.

Pure name classification over a records-category column name — no sidecar, no
DuckDB, no context. `records_column_role` is total over the four contract
column families (`design doc § Semantics — the records-column taxonomy`);
every other name is a no-role condition every caller must treat as loud
(conformance records a C5 failure; the source exporter raises
`SourceUnclassifiedColumn`) — never a silent fall-through.
"""

from __future__ import annotations

from typing import Final, Literal

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
