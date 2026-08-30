"""Output-name resolution and kind vocabulary for the streaming exporter.

Pure config+sidecar presentation resolution, shared by the engine's
after-image assembly and the driver's Debezium value-schema builders (the
single-producer discipline extended from column order to column naming):
both consumers read the same resolved `OutputEntry` list, so the declared
schema and the rendered rows cannot diverge.

The single naming authority: every published identity surface's output key
(`resolve_identity_output_key`) and every stream's full ordered after-image
naming (`resolve_stream_output_columns` / `resolve_membership_output_columns`)
resolve here, consumed by the engine's after-image assembly and by the
driver's Debezium value-schema builders alike.

Layer-direction invariant: imports derivations, config, the mode-neutral
routing surface, and errors — never the engine, drivers, writers, or CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

from fabulexa_forge.config.models import KindStream, MembershipStream
from fabulexa_forge.derivations.membership_events import resolve_membership_columns
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.errors import (
    StreamKindLabelCollision,
    StreamKindLabelUnknown,
    StreamOutputNameCollision,
    StreamRenameUnresolvable,
)

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface, MembershipRef, StreamConfig
    from fabulexa_forge.reader.sidecar import Sidecar

#: The Debezium membership envelope's reserved payload column — never
#: addressable by a membership stream's `rename`, regardless of format (the
#: config never knows its eventual format, so one eager rule covers both).
_MEMBERSHIP_EVENT_RESERVED = "event"

#: Every identity surface a `rename` key might name — used only to decide
#: whether an unresolved rename key gets the published-set suffix (it names
#: a surface that simply isn't published here) or not (a plain typo).
_KEY_SURFACE_NAMES: frozenset[str] = frozenset(
    {"record_id", "record_index", "presentation_id"}
)


@dataclass(frozen=True)
class IdentityProjection:
    """One stream's resolved, gated identity projection."""

    elected: "KeySurface"
    """The stream's gated uniform elected surface — for a membership stream,
    the owner's. Always a member of `published`."""

    published: "tuple[KeySurface, ...]"
    """Every surface this stream publishes, in the kind's sidecar column
    order (record_id, presentation_id, record_index). Never empty."""


@dataclass(frozen=True)
class OutputEntry:
    """One after-image entry: where its value comes from, and its wire name."""

    source_kind: 'Literal["identity", "payload"]'
    """'identity': `source` names a KeySurface rendered through its election
    relation (or, for record_id, the fold's own column). 'payload': `source`
    names a fold output column read verbatim."""

    source: str
    """The surface name or the fold column name, per `source_kind`."""

    output_key: str
    """The wire name — the bare default or the resolved rename target."""


def resolve_identity_output_key(
    rename: "Mapping[str, str] | None",
    surface: "KeySurface",
) -> str:
    """The wire name of one published identity surface.

    The single producer of every identity output key, consulted by the
    after-image resolvers and the message-key assembly site.

    Args:
        rename: The stream's declared rename map, or None.
        surface: A published surface.

    Returns:
        The rename target keyed on `surface` when the map carries one, else
        `surface` itself (the contract column name).
    """
    if rename and surface in rename:
        return rename[surface]
    return surface


def _validate_rename_keys(
    rename: "Mapping[str, str] | None",
    selected: frozenset[str],
    published: "tuple[KeySurface, ...]",
    noun: str,
) -> None:
    """Enforce StreamRenameUnresolvable: every rename key names a selection
    member or a published identity surface's contract column name.

    Args:
        rename: The stream's declared rename map, or None.
        selected: The stream's declared projection (`properties` / `fields`),
            as a set.
        published: The stream's published identity surfaces — each surface's
            own contract column name is a legal rename key.
        noun: 'property' or 'field', for the message.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a member of
            `selected` nor a published surface. When the key is itself a
            KeySurface name that this stream simply does not publish, the
            message appends the published set.
    """
    if not rename:
        return
    published_set = frozenset(published)
    for key in rename:
        if key in selected or key in published_set:
            continue
        if key in _KEY_SURFACE_NAMES:
            raise StreamRenameUnresolvable(
                f"rename key '{key}' names no selected {noun} and is not a"
                f" published identity surface (published: {sorted(published_set)})"
            )
        raise StreamRenameUnresolvable(f"rename key '{key}' names no selected {noun}")


def _record_output_entry(
    entries: list[OutputEntry],
    claimed: dict[str, str],
    source_kind: 'Literal["identity", "payload"]',
    source: str,
    output_key: str,
    label: str,
) -> None:
    """Append one resolved OutputEntry, gating collisions.

    The one place an output key is claimed — shared by every entry a
    resolver emits (each published identity surface and each payload
    column), so reserved names and payload names are checked against the
    same claim table.

    Args:
        entries: The resolver's accumulated result list, appended in place.
        claimed: output key -> the label of the entry that first claimed it,
            mutated in place.
        source_kind: 'identity' or 'payload'.
        source: The surface name (identity) or fold column name (payload).
        output_key: The entry's resolved output key.
        label: A human-readable description of this entry, used as `{other}`
            in a later collision this entry causes.

    Raises:
        StreamOutputNameCollision: `output_key` was already claimed.
    """
    if output_key in claimed:
        raise StreamOutputNameCollision(
            f"output name '{output_key}' collides with '{claimed[output_key]}'"
        )
    claimed[output_key] = label
    entries.append(
        OutputEntry(source_kind=source_kind, source=source, output_key=output_key)
    )


def resolve_stream_output_columns(
    sidecar: "Sidecar",
    kind: str,
    properties: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    identity: IdentityProjection,
) -> list[OutputEntry]:
    """Resolve a kind-shaped stream's after-image entries.

    The single naming authority. Order: published identity surfaces in
    sidecar column order, then selected properties in the column-order
    producer's order. No absorption branch — under a presentation_id
    election the surface is published once, as identity.

    Args:
        sidecar: The typed sidecar.
        kind: The stream's records kind, bare.
        properties: The stream's declared projection, bare names.
        rename: The stream's rename map, or None.
        identity: The stream's resolved, gated identity projection.

    Returns:
        Ordered OutputEntry list — the one list the after-image keying, the
        JSONL renderer, and the Debezium value schema all consume.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected
            property nor a published surface; message appends the published
            set only when the key is an unpublished surface name.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published identity key.
    """
    fold_columns = resolve_stream_columns(sidecar, kind, frozenset(properties))
    selected = frozenset(properties)
    _validate_rename_keys(rename, selected, identity.published, "property")

    entries: list[OutputEntry] = []
    claimed: dict[str, str] = {}
    for surface in identity.published:
        output_key = resolve_identity_output_key(rename, surface)
        _record_output_entry(
            entries, claimed, "identity", surface, output_key, "identity"
        )

    for fold_column in fold_columns:
        if fold_column in ("record_id", "presentation_id"):
            continue
        prop = fold_column[len("prop__") :]
        output_key = rename.get(prop, prop) if rename else prop
        _record_output_entry(
            entries, claimed, "payload", fold_column, output_key, f"property '{prop}'"
        )

    return entries


def resolve_membership_output_columns(
    sidecar: "Sidecar",
    membership: "MembershipRef",
    fields: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    owner_identity: IdentityProjection,
) -> list[OutputEntry]:
    """The membership analog: published owner identity surfaces in the owner
    kind's sidecar column order, then selected element fields in
    element-schema declaration order (never the config `fields` list's
    order) — a scalar field one entry, a reference field its `<f>_kind` /
    `<f>_id` pair renamed in place.

    Args:
        sidecar: The typed sidecar.
        membership: The stream's membership-table address.
        fields: The stream's declared field projection, bare names.
        rename: The stream's rename map, or None.
        owner_identity: The stream's resolved, gated owner identity
            projection.

    Returns:
        Ordered OutputEntry list.

    Raises:
        StreamRenameUnresolvable: A rename key names neither a selected
            field nor a published owner surface.
        StreamOutputNameCollision: Two output keys collide, or one collides
            with a published owner identity key or with `event`.
    """
    fold_columns = resolve_membership_columns(
        sidecar, membership.kind, membership.property, fields
    )
    selected = frozenset(fields)
    _validate_rename_keys(rename, selected, owner_identity.published, "field")

    entries: list[OutputEntry] = []
    claimed: dict[str, str] = {_MEMBERSHIP_EVENT_RESERVED: _MEMBERSHIP_EVENT_RESERVED}
    for surface in owner_identity.published:
        output_key = resolve_identity_output_key(rename, surface)
        _record_output_entry(
            entries, claimed, "identity", surface, output_key, "identity"
        )

    payload_columns = fold_columns[1:]
    i = 0
    while i < len(payload_columns):
        column = payload_columns[i]
        if column.startswith("member__") and column.endswith("__kind"):
            field = column[len("member__") : -len("__kind")]
            id_column = payload_columns[i + 1]
            target = rename.get(field, field) if rename else field
            _record_output_entry(
                entries,
                claimed,
                "payload",
                column,
                f"{target}_kind",
                f"field '{field}'",
            )
            _record_output_entry(
                entries,
                claimed,
                "payload",
                id_column,
                f"{target}_id",
                f"field '{field}'",
            )
            i += 2
        else:
            field = column[len("elem__") :]
            target = rename.get(field, field) if rename else field
            _record_output_entry(
                entries, claimed, "payload", column, target, f"field '{field}'"
            )
            i += 1

    return entries


def _stream_subject_kind(stream: "KindStream | MembershipStream") -> str:
    """The kind a stream's vocabulary claims range over: its own kind
    (kind-shaped) or its owner kind (membership-shaped).

    Args:
        stream: A declared stream (`KindStream` or `MembershipStream`).

    Returns:
        The bare subject kind name.
    """
    if isinstance(stream, KindStream):
        return stream.kind
    return stream.membership.kind


def resolve_stream_kind_vocabulary(
    config: "StreamConfig",
    sidecar: "Sidecar",
) -> Mapping[str, str]:
    """Validate the run's kind vocabulary — the config-level kind_labels
    map plus every per-stream kind_label — and return the declared value
    mapping.

    Injectivity beyond "two kinds map to one label" (already refused at
    parse time) reduces to two residual masquerade checks: a config-level
    label equal to an unlabeled kind's own verbatim name, and a per-stream
    `kind_label` equal to a *different* kind's rendered name (its label, or
    its verbatim name when unlabeled).

    Args:
        config: The stream config (kind_labels plus every per-stream
            kind_label).
        sidecar: The typed sidecar (the kind universe the integrity rules
            range over).

    Returns:
        The declared config-level (kind, label) pairs; callers render an
        undeclared kind verbatim (identity fall-through is caller-side —
        the total mapping is the pair of this map and that rule). A
        per-stream kind_label is validated here but never enters the
        mapping: the engine applies it on its own stream's envelope only.

    Raises:
        StreamKindLabelUnknown: A kind_labels key names no sidecar kind.
        StreamKindLabelCollision: A label or a per-stream kind_label equals
            a different kind's rendered name.
    """
    known_kinds = frozenset(sidecar.record_kinds())
    kind_labels = config.kind_labels or {}

    for kind in kind_labels:
        if kind not in known_kinds:
            raise StreamKindLabelUnknown(
                f"kind_labels: '{kind}' is not a kind in this emit"
            )

    labeled_kinds = frozenset(kind_labels)
    for label in kind_labels.values():
        if label in known_kinds and label not in labeled_kinds:
            raise StreamKindLabelCollision(
                f"kind_labels: label '{label}' collides with kind '{label}'"
            )

    rendered_name = {kind: kind_labels.get(kind, kind) for kind in known_kinds}

    for stream in config.streams:
        if stream.kind_label is None:
            continue
        subject_kind = _stream_subject_kind(stream)
        for other_kind in known_kinds:
            if other_kind == subject_kind:
                continue
            if stream.kind_label == rendered_name[other_kind]:
                raise StreamKindLabelCollision(
                    f"stream '{stream.name}': kind_label '{stream.kind_label}'"
                    f" collides with kind '{other_kind}'"
                )

    return dict(kind_labels)


def apply_kind_vocabulary(
    value: str | None, vocabulary: Mapping[str, str]
) -> str | None:
    """Map one member-kind after-image value through the declared vocabulary.

    Identity fall-through: a value matching no declared pair renders
    verbatim, `NULL` (None) stays `NULL`. The mapping is total, so a
    corrupted emit's mutated kind cell surfaces unchanged, never masked and
    never a render error.

    Args:
        value: The raw `member__<f>__kind` value, or None.
        vocabulary: The resolved config-level kind -> label mapping (from
            `resolve_stream_kind_vocabulary`).

    Returns:
        `vocabulary.get(value, value)`, or None when `value` is None.
    """
    if value is None:
        return None
    return vocabulary.get(value, value)


def resolve_stream_envelope_kind(
    kind_label: str | None,
    kind_vocabulary: Mapping[str, str],
    subject_kind: str,
) -> str:
    """Resolve one stream's envelope `kind` value, first match wins.

    Args:
        kind_label: The stream's own declared `kind_label`, or None.
        kind_vocabulary: The resolved config-level kind -> label mapping.
        subject_kind: The stream's own kind (kind-shaped) or owner kind
            (membership-shaped).

    Returns:
        `kind_label` when declared, else `kind_vocabulary`'s label for
        `subject_kind`, else `subject_kind` verbatim.
    """
    if kind_label is not None:
        return kind_label
    return kind_vocabulary.get(subject_kind, subject_kind)
