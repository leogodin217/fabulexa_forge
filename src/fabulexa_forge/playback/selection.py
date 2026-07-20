"""The resolved-selection seam: validate a PlaybackSelection, resolve its sets.

`resolve_selection` is the sole entry point — every later playback surface
("selected" anything) reads through its output, `ResolvedSelection`, an
internal runtime type built once at open. Sidecar-only: no data reads, so
every rule here is a schema question, never a data question.

Layer-direction invariant: imports only the reader, the derivations shared
property helpers, fabulexa_forge.playback.*, and stdlib. Never imports
exporters.* or config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

from fabulexa_forge.derivations.properties import has_presentation_id
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.types import (
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtomSelection,
)
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.slice_only import (
    is_exempt_discriminator,
    is_non_exempt_slice_only,
)


@dataclass(frozen=True)
class ResolvedRecordSelection:
    """One RecordAtomSelection resolved against the sidecar.

    properties is the effective ordered property tuple (the projection);
    full_properties is the kind's full fold-invocation property set — always
    the full tracked + constant + exempt-discriminator set, independent of
    what the caller selected.
    """

    kind: str
    sub_types: tuple[str, ...]
    properties: tuple[str, ...]
    full_properties: tuple[str, ...]
    record_ids: frozenset[str] | None
    has_presentation_id: bool
    discriminator_declared: bool


@dataclass(frozen=True)
class ResolvedMembershipSelection:
    """One MembershipAtomSelection resolved against the sidecar.

    fields is the effective ordered field tuple (the projection);
    full_fields is the table's full element-schema field set — always the
    full set, independent of what the caller selected.
    """

    owner_kind: str
    property_name: str
    owner_sub_types: tuple[str, ...]
    fields: tuple[str, ...]
    full_fields: tuple[str, ...]
    owner_record_ids: frozenset[str] | None
    owner_discriminator_declared: bool


@dataclass(frozen=True)
class ResolvedSelection:
    """A PlaybackSelection resolved against one sidecar at open.

    Carries, per record selection: the effective ordered property tuple (full-set
    None resolved to tracked + constant + the exempt discriminator, sidecar
    declaration order), the kind's full fold-invocation property set, sub-type
    predicate values, instance ids, presentation-id presence, and discriminator
    declaredness; per membership selection: the effective ordered field tuple,
    the table's full element-schema field set, owner predicate values, instance
    ids, and owner-discriminator declaredness. Built by resolve_selection; every
    later "selected" means these resolved sets.
    """

    records: tuple[ResolvedRecordSelection, ...]
    memberships: tuple[ResolvedMembershipSelection, ...]


def _check_selection_non_empty(selection: PlaybackSelection) -> None:
    """SelectionNonEmpty: records + memberships name at least one selection."""
    if not selection.records and not selection.memberships:
        raise PlaybackError("playback selection is empty")


def _check_atoms_unique(selection: PlaybackSelection) -> None:
    """AtomsUnique: at most one RecordAtomSelection per kind, one
    MembershipAtomSelection per (owner_kind, property_name)."""
    seen_kinds: set[str] = set()
    for record_sel in selection.records:
        if record_sel.kind in seen_kinds:
            raise PlaybackError(f"duplicate selection for {record_sel.kind!r}")
        seen_kinds.add(record_sel.kind)

    seen_memberships: set[tuple[str, str]] = set()
    for membership_sel in selection.memberships:
        identity = (membership_sel.owner_kind, membership_sel.property_name)
        if identity in seen_memberships:
            raise PlaybackError(f"duplicate selection for {identity!r}")
        seen_memberships.add(identity)


def _check_instance_ids_non_empty(ids: frozenset[str] | None, field_name: str) -> None:
    """InstanceSetNonEmpty: record_ids / owner_record_ids is None or non-empty."""
    if ids is not None and not ids:
        raise PlaybackError(f"empty {field_name} — pass None for no restriction")


def _require_record_kind_columns(
    sidecar: "Sidecar", kind: str
) -> "tuple[ColumnSpec, ...]":
    """RecordKindResolvable: records__<kind> must be in the sidecar."""
    try:
        return sidecar.columns(f"records__{kind}")
    except TableNotFoundError as exc:
        raise PlaybackError(f"unknown kind {kind!r}") from exc


def _require_membership_table_columns(
    sidecar: "Sidecar", owner_kind: str, property_name: str
) -> "tuple[ColumnSpec, ...]":
    """MembershipResolvable: membership__<owner_kind>__<property_name> must
    be in the sidecar."""
    table_name = f"membership__{owner_kind}__{property_name}"
    try:
        return sidecar.columns(table_name)
    except TableNotFoundError as exc:
        raise PlaybackError(
            f"no membership table for {owner_kind!r}.{property_name!r}"
        ) from exc


def _resolve_subtypes(
    sidecar: "Sidecar",
    kind: str,
    cols: "tuple[ColumnSpec, ...]",
    sub_types: tuple[str, ...],
) -> bool:
    """SubTypesDeclared / OwnerSubTypesDeclared (shared shape): validate a
    sub_types predicate against one kind's discriminator.

    Returns whether the kind's discriminator column (prop__<kind>_type) is
    declared — needed regardless of whether sub_types names anything.
    """
    discriminator_declared = any(col.name == f"prop__{kind}_type" for col in cols)
    if not sub_types:
        return discriminator_declared

    declared_values = sidecar.subtype_values(kind)
    if not declared_values:
        raise PlaybackError(f"kind {kind!r} is not sub-typed")
    if not discriminator_declared:
        raise PlaybackError(f"kind {kind!r} lacks its discriminator column")

    seen: set[str] = set()
    for value in sub_types:
        if value in seen:
            raise PlaybackError(f"kind {kind!r} names duplicate sub-type {value!r}")
        seen.add(value)
        if value not in declared_values:
            raise PlaybackError(f"kind {kind!r} declares no sub-type {value!r}")
    return discriminator_declared


def _full_record_properties(
    sidecar: "Sidecar", kind: str, cols: "tuple[ColumnSpec, ...]", table_name: str
) -> tuple[str, ...]:
    """The kind's full fold-invocation property set, sidecar declaration order:
    every tracked + constant property, plus the exempt discriminator whatever
    its class."""
    full: list[str] = []
    for col in cols:
        if not col.name.startswith("prop__"):
            continue
        prop = col.name[len("prop__") :]
        if is_exempt_discriminator(sidecar, kind, col.name):
            full.append(prop)
            continue
        if sidecar.temporal_class(table_name, col.name) in ("tracked", "constant"):
            full.append(prop)
    return tuple(full)


def _validate_named_properties(
    sidecar: "Sidecar",
    kind: str,
    cols: "tuple[ColumnSpec, ...]",
    properties: tuple[str, ...],
) -> None:
    """PropertiesResolvable + PropertiesNotSliceOnly over a named tuple."""
    prop_col_names = {col.name for col in cols if col.name.startswith("prop__")}
    seen: set[str] = set()
    for name in properties:
        if name in seen:
            raise PlaybackError(f"duplicate property {name!r}")
        seen.add(name)
        col_name = f"prop__{name}"
        if col_name not in prop_col_names:
            raise PlaybackError(f"kind {kind!r} has no property {name!r}")
        if is_non_exempt_slice_only(sidecar, kind, col_name):
            raise PlaybackError(
                f"property {name!r} on kind {kind!r} is slice_only — "
                "its value at T is unknowable"
            )


def _resolve_record_properties(
    sidecar: "Sidecar",
    kind: str,
    cols: "tuple[ColumnSpec, ...]",
    table_name: str,
    properties: tuple[str, ...] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve a RecordAtomSelection.properties axis.

    Returns (effective_properties, full_properties): full_properties is
    always the kind's full fold-invocation set; effective_properties is the
    caller's projection over it (None -> full set; () -> identity only; a
    named tuple -> validated and reordered to sidecar declaration order).
    """
    full = _full_record_properties(sidecar, kind, cols, table_name)
    if properties is None:
        return full, full
    if not properties:
        return (), full
    _validate_named_properties(sidecar, kind, cols, properties)
    named = frozenset(properties)
    effective = tuple(prop for prop in full if prop in named)
    return effective, full


def _full_membership_field_names(cols: "tuple[ColumnSpec, ...]") -> tuple[str, ...]:
    """The membership table's full element-schema field set: bare field
    names, sidecar declaration order — a reference field counted once, keyed
    off its member__<f>__kind column."""
    result: list[str] = []
    seen: set[str] = set()
    for col in cols:
        name = col.name
        if name.startswith("member__") and name.endswith("__kind"):
            field = name[len("member__") : -len("__kind")]
        elif name.startswith("elem__"):
            field = name[len("elem__") :]
        else:
            continue
        if field not in seen:
            result.append(field)
            seen.add(field)
    return tuple(result)


def _validate_named_fields(
    owner_kind: str,
    property_name: str,
    cols: "tuple[ColumnSpec, ...]",
    fields: tuple[str, ...],
) -> None:
    """MembershipFieldsResolvable over a named tuple."""
    scalar_names = {
        col.name[len("elem__") :] for col in cols if col.name.startswith("elem__")
    }
    ref_names = {
        col.name[len("member__") : -len("__kind")]
        for col in cols
        if col.name.startswith("member__") and col.name.endswith("__kind")
    }
    valid_names = scalar_names | ref_names
    seen: set[str] = set()
    for name in fields:
        if name in seen:
            raise PlaybackError(f"duplicate field {name!r}")
        seen.add(name)
        if name not in valid_names:
            raise PlaybackError(
                f"membership {owner_kind!r}.{property_name!r} has no field {name!r}"
            )


def _resolve_membership_fields(
    owner_kind: str,
    property_name: str,
    cols: "tuple[ColumnSpec, ...]",
    fields: tuple[str, ...] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve a MembershipAtomSelection.fields axis.

    Returns (effective_fields, full_fields): full_fields is always the
    table's full element-schema field set; effective_fields is the caller's
    projection over it.
    """
    full = _full_membership_field_names(cols)
    if fields is None:
        return full, full
    if not fields:
        return (), full
    _validate_named_fields(owner_kind, property_name, cols, fields)
    named = frozenset(fields)
    effective = tuple(field for field in full if field in named)
    return effective, full


def _resolve_record_atom_selection(
    sidecar: "Sidecar", selection: RecordAtomSelection
) -> ResolvedRecordSelection:
    """Apply every record-atom business rule and resolve its effective sets."""
    cols = _require_record_kind_columns(sidecar, selection.kind)
    table_name = f"records__{selection.kind}"
    discriminator_declared = _resolve_subtypes(
        sidecar, selection.kind, cols, selection.sub_types
    )
    _check_instance_ids_non_empty(selection.record_ids, "record_ids")
    properties, full_properties = _resolve_record_properties(
        sidecar, selection.kind, cols, table_name, selection.properties
    )
    return ResolvedRecordSelection(
        kind=selection.kind,
        sub_types=selection.sub_types,
        properties=properties,
        full_properties=full_properties,
        record_ids=selection.record_ids,
        has_presentation_id=has_presentation_id(sidecar, selection.kind),
        discriminator_declared=discriminator_declared,
    )


def _resolve_membership_atom_selection(
    sidecar: "Sidecar", selection: MembershipAtomSelection
) -> ResolvedMembershipSelection:
    """Apply every membership-atom business rule and resolve its effective sets."""
    cols = _require_membership_table_columns(
        sidecar, selection.owner_kind, selection.property_name
    )
    owner_cols = sidecar.columns(f"records__{selection.owner_kind}")
    owner_discriminator_declared = _resolve_subtypes(
        sidecar, selection.owner_kind, owner_cols, selection.owner_sub_types
    )
    _check_instance_ids_non_empty(selection.owner_record_ids, "owner_record_ids")
    fields, full_fields = _resolve_membership_fields(
        selection.owner_kind, selection.property_name, cols, selection.fields
    )
    return ResolvedMembershipSelection(
        owner_kind=selection.owner_kind,
        property_name=selection.property_name,
        owner_sub_types=selection.owner_sub_types,
        fields=fields,
        full_fields=full_fields,
        owner_record_ids=selection.owner_record_ids,
        owner_discriminator_declared=owner_discriminator_declared,
    )


def resolve_selection(
    sidecar: "Sidecar",
    selection: PlaybackSelection,
) -> ResolvedSelection:
    """Validate a selection against the sidecar and resolve its effective sets.

    Applies every selection business rule (design doc § Validation Rules:
    SelectionNonEmpty, RecordKindResolvable, SubTypesDeclared,
    PropertiesResolvable, PropertiesNotSliceOnly, MembershipResolvable,
    OwnerSubTypesDeclared, MembershipFieldsResolvable, AtomsUnique,
    InstanceSetNonEmpty) — sidecar-only, no data reads.

    Args:
        sidecar: The open emit's sidecar.
        selection: The caller's atom selection.

    Returns:
        The resolved selection.

    Raises:
        PlaybackError: Any rule fails; messages per the doc's rule table.
    """
    _check_selection_non_empty(selection)
    _check_atoms_unique(selection)
    records = tuple(
        _resolve_record_atom_selection(sidecar, record_sel)
        for record_sel in selection.records
    )
    memberships = tuple(
        _resolve_membership_atom_selection(sidecar, membership_sel)
        for membership_sel in selection.memberships
    )
    return ResolvedSelection(records=records, memberships=memberships)
