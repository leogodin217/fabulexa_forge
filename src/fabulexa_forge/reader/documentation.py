"""The documentation view: one typed surface over an emit's five documentation
sources — scenario narrative, table prose, per-column description/unit, and
declared-value glosses.

Permissive verbatim carry: nothing is validated, nothing is inferred, absence
is silence. One authority answers each declared column — the vendored
contract's pinned structural-column strings for structural columns (identity /
presentation / lifecycle names, `ref_index__<name>`, the fixed `history`
table's columns, the membership family's structural names), the sidecar
verbatim for every other declared column — never both, never a fallback
(design doc § The documentation view).

Constructed only by `Sidecar.documentation()`; not constructed directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping

from fabulexa_forge.reader._enum_domains import parse_enum_domains_glossed
from fabulexa_forge.reader.records_columns import REF_INDEX_PREFIX

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import Sidecar, TableSpec


@dataclass(frozen=True)
class ColumnDoc:
    """Resolved documentation for one declared column.

    origin names the single authority that answered: "contract" for a
    structural column (pinned strings, instance placeholders bound),
    "sidecar" for a per-run column (verbatim carry), "author" for a
    companion-dictionary resolution answered by the export config's
    per-column description override, "forge" for a companion-dictionary
    resolution answered by the forge-pinned event-log column set. This
    reader's documentation view never produces "author" or "forge" — both
    are stamped only downstream, by the companion dictionary.
    """

    description: str | None
    unit: str | None
    origin: Literal["contract", "sidecar", "author", "forge"]


@dataclass(frozen=True)
class EnumOption:
    """One declared allowed value of a closed-domain property."""

    value: str
    description: str | None


#: The vendored contract's § Structural column descriptions block, pinned
#: verbatim (contract/base-format.md), keyed by table family then column
#: name — the same hardcoding class as the pinned column lists and the
#: table-category enum in sidecar.py; re-synced on contract re-vendor. Each
#: entry is (description, unit-or-None). The `ref_index__<name>` entry is a
#: template key, matched by column-name prefix, not by equality.
_STRUCTURAL_COLUMN_DOCS: Mapping[str, Mapping[str, tuple[str, str | None]]] = {
    "history": {
        "fork_path": (
            "Branch this row is attributed to (canonical @-joined branch path).",
            None,
        ),
        "kind": ("Kind of the record whose property changed.", None),
        "record_id": (
            "Id of the record whose property changed. Opaque; equality-join "
            "against records__<kind>.record_id.",
            None,
        ),
        "property": ("Name of the property that changed.", None),
        "sim_time": (
            "Simulation time the change took effect; the value holds until "
            "the series' next row.",
            "ns",
        ),
        "value": (
            "Value in effect from sim_time onward, text-encoded. NULL only "
            "in a creation-seed row for a property with no creation value.",
            None,
        ),
    },
    "records": {
        "fork_path": (
            "Branch this row is attributed to (canonical @-joined branch path).",
            None,
        ),
        "record_id": (
            "Opaque identifier of the record within its branch and kind. Not "
            "ordered by creation; use record_index for creation order.",
            None,
        ),
        "presentation_id": (
            "Presentation surrogate identity minted for this kind; present "
            "only when the sidecar declares it.",
            None,
        ),
        "created_sim_time": (
            "Simulation time the record was created. Set once; never "
            "changed by later writes or deactivation.",
            "ns",
        ),
        "active": (
            "Record-existence flag: FALSE iff the record was deactivated "
            "before the slice boundary. Lifecycle only — not the domain's "
            "on/off state.",
            None,
        ),
        "deactivated_at": (
            "Simulation time the record was deactivated; NULL while active.",
            "ns",
        ),
        "last_mutation_sim_time": (
            "Simulation time of the record's most recent content change: "
            "creation, any property write, or deactivation.",
            "ns",
        ),
        "record_index": (
            "Dense 0-based creation-order ordinal of the record within its "
            "(fork_path, kind). Stable across emits of the same branch; "
            "direct positional key for creation order.",
            None,
        ),
        "ref_index__<name>": (
            "Creation-order index of the record referenced by the sibling "
            "prop__<name> column, resolved at the emitted slice; NULL "
            "together with it.",
            None,
        ),
    },
    "membership": {
        "fork_path": (
            "Branch this row is attributed to (canonical @-joined branch path).",
            None,
        ),
        "record_id": (
            "Id of the record that owns the collection; its kind is the "
            "table name's <K> segment.",
            None,
        ),
        "joined_sim_time": (
            "Simulation time the element entered the collection.",
            "ns",
        ),
        "left_sim_time": (
            "Simulation time the element left the collection; NULL while "
            "still present at the slice boundary.",
            "ns",
        ),
    },
}


def _pinned_family_for_category(category: str) -> str | None:
    """The pinned block's family key for a table category, or None.

    Args:
        category: A sidecar table category ("fixed", "records", "membership").

    Returns:
        The matching `_STRUCTURAL_COLUMN_DOCS` key, or None for a category
        the pinned block does not cover.
    """
    if category == "fixed":
        return "history"
    if category in ("records", "membership"):
        return category
    return None


def _structural_lookup_key(family: str, column_name: str) -> tuple[str, str | None]:
    """The pinned block's lookup key for a column, and its `<name>` binding.

    A `ref_index__<name>` records column keys on the template literal
    `ref_index__<name>` and binds `<name>` from its own name suffix; every
    other column keys and binds by its own name (a no-op bind when the
    pinned string carries no `<name>`).

    Args:
        family: The table's pinned-block family ("history", "records",
            "membership").
        column_name: The declared column name.

    Returns:
        (lookup_key, name_binding).
    """
    if family == "records" and column_name.startswith(REF_INDEX_PREFIX):
        return "ref_index__<name>", column_name[len(REF_INDEX_PREFIX) :]
    return column_name, None


def _substitute_placeholders(text: str, *, name: str | None, kind: str | None) -> str:
    """Bind a pinned contract string's instance placeholders.

    `<name>` binds to the referenced property's name; `<K>` / `<kind>` bind
    to the table's owning kind. A placeholder absent from `text` is a no-op —
    callers pass whichever bindings the family may use and rely on this to
    ignore the ones that don't apply.

    Args:
        text: The pinned description string.
        name: The `<name>` binding, or None to leave `<name>` untouched.
        kind: The `<K>` / `<kind>` binding, or None to leave them untouched.

    Returns:
        The string with every bound placeholder substituted.
    """
    if name is not None:
        text = text.replace("<name>", name)
    if kind is not None:
        text = text.replace("<K>", kind).replace("<kind>", kind)
    return text


def _resolve_structural_doc(table: "TableSpec", column_name: str) -> ColumnDoc | None:
    """Resolve a column's documentation against the pinned contract block.

    Args:
        table: The column's declared table.
        column_name: The declared column name.

    Returns:
        The contract-answered ColumnDoc, or None when the column's (family,
        name) carries no pinned string — the sidecar answers instead.
    """
    family = _pinned_family_for_category(table.category)
    if family is None:
        return None
    pinned = _STRUCTURAL_COLUMN_DOCS[family]
    lookup_key, name_binding = _structural_lookup_key(family, column_name)
    entry = pinned.get(lookup_key)
    if entry is None:
        return None
    description, unit = entry
    if family != "history":
        description = _substitute_placeholders(
            description, name=name_binding, kind=table.record_kind
        )
    return ColumnDoc(description=description, unit=unit, origin="contract")


class Documentation:
    """Typed, read-only documentation view over one emit's five surfaces.

    Permissive verbatim carry — nothing validated, nothing inferred,
    absence is silence. Constructed by Sidecar.documentation(); not
    constructed directly. Resolution rule: design doc § The documentation
    view (one authority per column — the vendored contract strings for
    structural columns, keyed by the reader's structural taxonomy; the
    sidecar entry for every other declared column, taxonomy-no-role
    columns included; never both, never a fallback).
    """

    def __init__(self, sidecar: "Sidecar") -> None:
        self._sidecar = sidecar
        self._enum_domains_glossed = parse_enum_domains_glossed(
            sidecar.raw.get("enum_domains")
        )

    def scenario_description(self) -> str | None:
        """The run's declared narrative, verbatim; None when absent."""
        value = self._sidecar.raw.get("scenario_description")
        return value if isinstance(value, str) else None

    def table_description(self, table_name: str) -> str | None:
        """One table's tables[].description, verbatim.

        Args:
            table_name: A table the sidecar declares.

        Returns:
            The description, or None when the table carries none (always
            None for the fixed `history` table, per the contract).

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
        """
        return self._sidecar.table(table_name).description

    def column_doc(self, table_name: str, column_name: str) -> ColumnDoc | None:
        """Resolve one declared column's documentation.

        Structural columns (per the reader's structural taxonomy) answer
        from the vendored contract strings with instance-bound placeholders
        substituted (`<name>` from the column name, `<K>`/`<kind>` from the
        table; `history`-family placeholders stay verbatim); every other
        column answers from its sidecar entry.

        Args:
            table_name: A table the sidecar declares.
            column_name: A column that table declares.

        Returns:
            The resolved ColumnDoc; None when a per-run column carries
            neither description nor unit. Structural columns always answer.

        Raises:
            TableNotFoundError: table_name is not declared by the sidecar.
            ColumnNotFoundError: column_name is not declared by that table.
        """
        table = self._sidecar.table(table_name)
        col = self._sidecar.column(table_name, column_name)
        structural = _resolve_structural_doc(table, column_name)
        if structural is not None:
            return structural
        if col.description is None and col.unit is None:
            return None
        return ColumnDoc(description=col.description, unit=col.unit, origin="sidecar")

    def enum_options(self, kind: str, prop: str) -> tuple[EnumOption, ...]:
        """The ordered declared value objects of one closed-domain property.

        Membership and order equal the typed values-only enum_domains
        surface — one parse floor shared by both views (a malformed value
        object drops whole from both; a mis-shaped gloss parses as
        gloss-absent). A sub-typed kind's discriminator is `<kind>_type`.

        Args:
            kind: A kind with an enum_domains entry.
            prop: A property in that kind's entry.

        Returns:
            The declared options in sidecar order, glosses verbatim.

        Raises:
            KeyError: (kind, prop) has no enum_domains entry.
        """
        pairs = self._enum_domains_glossed[kind][prop]
        return tuple(
            EnumOption(value=value, description=gloss) for value, gloss in pairs
        )
