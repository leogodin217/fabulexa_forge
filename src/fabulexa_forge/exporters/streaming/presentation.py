"""Output-name resolution and kind vocabulary for the streaming exporter.

Pure config+sidecar presentation resolution, shared by the engine's
after-image assembly and the driver's Debezium value-schema builders (the
single-producer discipline extended from column order to column naming):
both consumers read the same resolved `(fold column, output key)` list, so
the declared schema and the rendered rows cannot diverge.

Subsumes and replaces `engine.elect_after_image_columns` (the identity
re-key / presentation_id-absorption rule folds into the leading pair each
resolver returns) and `engine._rekey_after_image` (the engine assembles
after-images by keying dicts directly off the resolved pairs, never by a
separate re-key pass).

Layer-direction invariant: imports derivations, config, the mode-neutral
routing surface, and errors — never the engine, drivers, writers, or CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Sequence

from fabulexa_forge.config.models import KindStream, MembershipStream
from fabulexa_forge.derivations.membership_events import resolve_membership_columns
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.errors import (
    StreamKindLabelCollision,
    StreamKindLabelUnknown,
    StreamOutputNameCollision,
    StreamRenameUnresolvable,
)
from fabulexa_forge.exporters.streaming.routing import known_records_kinds

if TYPE_CHECKING:
    from fabulexa_forge.config.models import MembershipRef, StreamConfig
    from fabulexa_forge.reader.sidecar import Sidecar

#: The Debezium membership envelope's reserved payload column — never
#: addressable by a membership stream's `rename`, regardless of format (the
#: config never knows its eventual format, so one eager rule covers both).
_MEMBERSHIP_EVENT_RESERVED = "event"


def _validate_rename_keys(
    rename: "Mapping[str, str] | None",
    selected: frozenset[str],
    noun: str,
) -> None:
    """Enforce StreamRenameUnresolvable: every rename key names a selection member.

    Args:
        rename: The stream's declared rename map, or None.
        selected: The stream's declared projection (`properties` / `fields`),
            as a set.
        noun: 'property' or 'field', for the message.

    Raises:
        StreamRenameUnresolvable: A rename key is not a member of `selected`.
    """
    if not rename:
        return
    for key in rename:
        if key not in selected:
            raise StreamRenameUnresolvable(
                f"rename key '{key}' names no selected {noun}"
            )


def _record_output_pair(
    pairs: list[tuple[str, str]],
    claimed: dict[str, str],
    fold_column: str,
    output_key: str,
    label: str,
) -> None:
    """Append one resolved (fold column, output key) pair, gating collisions.

    The one place an output key is claimed — shared by every entry a
    resolver emits (the identity entry, the presentation_id entry, and each
    payload column), so reserved names and payload names are checked against
    the same claim table.

    Args:
        pairs: The resolver's accumulated result list, appended in place.
        claimed: output key -> the label of the entry that first claimed it,
            mutated in place.
        fold_column: The fold's own column name for this entry.
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
    pairs.append((fold_column, output_key))


def resolve_stream_output_columns(
    sidecar: "Sidecar",
    kind: str,
    properties: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    identity_key: str,
) -> list[tuple[str, str]]:
    """Resolve a kind-shaped stream's after-image (fold column, output key)
    pairs — the single naming authority extending resolve_stream_columns.

    Order is resolve_stream_columns order exactly (identity entry, then
    presentation_id when carried and not absorbed, then projected properties
    in sidecar order); the identity entry's output key is `identity_key`,
    payload columns take their bare name or their rename target.

    Args:
        sidecar: The typed sidecar.
        kind: The stream's records kind, bare.
        properties: The stream's declared projection, bare names.
        rename: The stream's rename map, or None.
        identity_key: The identity entry's output key — the stream's elected
            surface's contract column name (record_id / record_index /
            presentation_id), resolved by the caller from the stream's
            election with absorption applied. Defines the reserved-name set
            together with presentation_id, reserved when the kind carries
            one and identity_key is not presentation_id (the unabsorbed
            case).

    Returns:
        Ordered (fold column name, output key) pairs — the one list the
        after-image keying, the JSONL renderer, and the Debezium value
        schema all consume.

    Raises:
        StreamRenameUnresolvable: A rename key names no selected property.
        StreamOutputNameCollision: Two output keys collide, or an output key
            collides with a reserved identity name.
    """
    fold_columns = resolve_stream_columns(sidecar, kind, frozenset(properties))
    selected = frozenset(properties)
    _validate_rename_keys(rename, selected, "property")

    has_presentation_id = "presentation_id" in fold_columns
    absorbed = has_presentation_id and identity_key == "presentation_id"

    pairs: list[tuple[str, str]] = []
    claimed: dict[str, str] = {}
    _record_output_pair(pairs, claimed, "record_id", identity_key, "identity")

    for fold_column in fold_columns[1:]:
        if fold_column == "presentation_id":
            if absorbed:
                continue
            _record_output_pair(
                pairs, claimed, fold_column, "presentation_id", "presentation_id"
            )
            continue
        prop = fold_column[len("prop__") :]
        output_key = rename.get(prop, prop) if rename else prop
        _record_output_pair(
            pairs, claimed, fold_column, output_key, f"property '{prop}'"
        )

    return pairs


def resolve_membership_output_columns(
    sidecar: "Sidecar",
    membership: "MembershipRef",
    fields: "Sequence[str]",
    rename: "Mapping[str, str] | None",
    owner_identity_key: str,
) -> list[tuple[str, str]]:
    """The membership analog of resolve_stream_output_columns, extending
    resolve_membership_columns. Order is resolve_membership_columns order
    exactly: owner identity entry, then selected element fields in
    element-schema declaration order (never the config `fields` list's
    order) — a scalar field one pair, a reference field its `<f>_kind` /
    `<f>_id` pair renamed in place.

    Args:
        sidecar: The typed sidecar.
        membership: The stream's membership-table address.
        fields: The stream's declared field projection, bare names.
        rename: The stream's rename map, or None.
        owner_identity_key: The owner identity entry's output key — the
            owner's elected surface's contract column name, resolved by the
            caller. With the membership `event` name, defines the reserved
            set.

    Returns:
        Ordered (fold column name, output key) pairs.

    Raises:
        StreamRenameUnresolvable: A rename key names no selected field.
        StreamOutputNameCollision: Two output keys collide, or an output key
            collides with the owner identity entry or the reserved
            membership `event` name.
    """
    fold_columns = resolve_membership_columns(
        sidecar, membership.kind, membership.property, fields
    )
    selected = frozenset(fields)
    _validate_rename_keys(rename, selected, "field")

    pairs: list[tuple[str, str]] = []
    claimed: dict[str, str] = {_MEMBERSHIP_EVENT_RESERVED: _MEMBERSHIP_EVENT_RESERVED}
    _record_output_pair(pairs, claimed, "record_id", owner_identity_key, "identity")

    payload_columns = fold_columns[1:]
    i = 0
    while i < len(payload_columns):
        column = payload_columns[i]
        if column.startswith("member__") and column.endswith("__kind"):
            field = column[len("member__") : -len("__kind")]
            id_column = payload_columns[i + 1]
            target = rename.get(field, field) if rename else field
            _record_output_pair(
                pairs, claimed, column, f"{target}_kind", f"field '{field}'"
            )
            _record_output_pair(
                pairs, claimed, id_column, f"{target}_id", f"field '{field}'"
            )
            i += 2
        else:
            field = column[len("elem__") :]
            target = rename.get(field, field) if rename else field
            _record_output_pair(pairs, claimed, column, target, f"field '{field}'")
            i += 1

    return pairs


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
    known_kinds = frozenset(known_records_kinds(sidecar))
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
