"""The atom-selection surface: identity types and the caller-facing selection.

Named apart from streaming's config-level `MembershipSelection`: the playback
pair carries the `Atom` infix deliberately — one name never means two shapes.

Layer-direction invariant: imports nothing but stdlib. Never imports
exporters.*, config, or the reader.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordAtom:
    """One record population: a sub-type of a kind, or a whole non-sub-typed kind.

    sub_type is None when the kind declares no discriminator domain, when
    the record's discriminator cell is NULL (a corrupted tape), or when
    the sidecar does not declare the discriminator column (a drifted
    tape). On a corrupted tape it may hold an undeclared value verbatim —
    the stamp is data; the declared domain is only the selection
    vocabulary.
    """

    kind: str
    sub_type: str | None


@dataclass(frozen=True)
class MembershipAtom:
    """One membership population: an owner population's collection property.

    owner_sub_type is None when the owner kind declares no discriminator
    domain, when the owner has no spine row (a corrupted tape's orphan
    membership row — played verbatim, never dropped), or when the owner's
    discriminator cell is NULL or its column is undeclared (a drifted
    tape). May hold an undeclared value verbatim on a corrupted tape.
    """

    owner_kind: str
    owner_sub_type: str | None
    property_name: str


@dataclass(frozen=True)
class RecordAtomSelection:
    """Select record populations of one kind, with properties and instances.

    sub_types: declared discriminator values to include — a predicate over
        the spine discriminator; the empty tuple means the whole kind (no
        discriminator filter; the bare kind when not sub-typed). Non-empty is
        legal only for a sub-typed kind whose discriminator column the
        sidecar declares (the drifted-tape rule).
    properties: bare property names riding after-images and snapshot rows, of
        temporal class tracked or constant — a non-exempt slice_only
        property fails at open (the shipped export-wide predicate; the
        exempt sub-typed discriminator is selectable, any class); the
        empty tuple means
        identity + lifecycle only; None means the full selectable set —
        every tracked + constant property plus the exempt discriminator,
        resolved at open (never a non-exempt
        slice_only column). Projection only — never changes the event
        row set or seq.
    record_ids: the instance axis — restrict to these record ids; None means
        no instance restriction. Must be non-empty when given. Unknown ids
        select nothing (never an error).
    """

    kind: str
    sub_types: tuple[str, ...]
    properties: tuple[str, ...] | None
    record_ids: frozenset[str] | None


@dataclass(frozen=True)
class MembershipAtomSelection:
    """Select one membership table, with owner populations and instances.

    owner_sub_types: declared owner discriminator values to include — a spine
        predicate (an orphan owner matches no named value); empty tuple = all
        owners, orphans included. Non-empty is legal only for a sub-typed
        owner kind whose discriminator column the sidecar declares (the
        drifted-tape rule).
    fields: bare element-schema field names riding payloads and containment
        rows; empty tuple = owner identity only; None = the full
        element-schema field set, resolved at open. Projection only — never
        changes the event row set or seq.
    owner_record_ids: restrict to these owner ids; None = no restriction.
        Must be non-empty when given. Unknown ids select nothing.
    """

    owner_kind: str
    owner_sub_types: tuple[str, ...]
    property_name: str
    fields: tuple[str, ...] | None
    owner_record_ids: frozenset[str] | None


@dataclass(frozen=True)
class PlaybackSelection:
    """The head's full atom selection.

    At most one RecordAtomSelection per kind and one MembershipAtomSelection per
    (owner_kind, property_name); at least one selection overall.
    """

    records: tuple[RecordAtomSelection, ...]
    memberships: tuple[MembershipAtomSelection, ...]
