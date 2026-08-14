"""The playback event stream: canonical total order, seq, atom stamping.

`PlaybackEvent` plus the pull-only iterator that k-way merges every selected
atom's fold rows (record and membership alike) into the seam's canonical
total order (design doc § The canonical total order and entry-point-invariant
`seq`), assigns `seq` over the whole in-scope stream before any bound filter
is applied, and renders `ts` through the shared anchor renderer.

Layer-direction invariant: imports the reader, the derivations event folds,
`fabulexa_forge.anchor`, `fabulexa_forge.playback.*`, and stdlib. Never
imports exporters.* or config.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Literal, cast

from fabulexa_forge.anchor import render_ts
from fabulexa_forge.derivations.membership_events import (
    build_membership_events_sql,
)
from fabulexa_forge.derivations.membership_events import (
    fold_row_column_names as membership_fold_row_column_names,
)
from fabulexa_forge.derivations.row_state_events import (
    build_row_state_events_sql,
)
from fabulexa_forge.derivations.row_state_events import (
    fold_row_column_names as record_fold_row_column_names,
)
from fabulexa_forge.playback.stamp import spine_discriminator_index
from fabulexa_forge.playback.types import MembershipAtom, RecordAtom

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.playback.selection import (
        ResolvedMembershipSelection,
        ResolvedRecordSelection,
        ResolvedSelection,
    )
    from fabulexa_forge.reader.emit import Emit

# Indices into a fold output row (record and membership folds share this
# fixed four-column prefix): record_id, event_sim_time, event_class, op.
_IDX_RECORD_ID = 0
_IDX_EVENT_SIM_TIME = 1
_IDX_EVENT_CLASS = 2
_IDX_OP = 3

# Family rank: record events precede membership events on a class tie.
_FAMILY_RECORD = 0
_FAMILY_MEMBERSHIP = 1

#: The canonical merge key: (event_sim_time, event_class, family, identity,
#: record_id). identity is a same-arity tuple within a family — (kind,) for
#: record events, (owner_kind, property_name) for membership events — so
#: family already discriminates before identity is ever compared, and no
#: string-flattening of the membership identity risks colliding with a kind.
_EventKey = tuple[int, int, int, "tuple[str, ...]", str]

#: One merged-stream row, pre-built up to (not including) its final seq.
_EventRow = tuple[
    _EventKey,
    Literal["c", "u", "d", "join", "leave"],
    "RecordAtom | MembershipAtom",
    str,
    str | None,
    int,
    str | int,
    "dict[str, str | None] | None",
]


@dataclass(frozen=True)
class PlaybackEvent:
    """One ordered change event on the seam's canonical event-time line.

    seq: 1-based position in the canonical total order over the whole in-scope
        stream — a pure function of (tape, selection), entry-point-invariant.
    op: 'c'/'u'/'d' for record events; 'join'/'leave' for membership events.
    atom: the population the event belongs to, sub-type resolved per record.
    record_id: the changed record's natural id, or the membership owner's id;
        the event key.
    presentation_id: the record's surrogate when the kind carries one; always
        None for membership events. Never the key.
    event_sim_time: the raw event-time key (ns).
    ts: offset-bearing ISO-8601 str when the head's anchor resolves, else the
        raw event_sim_time int.
    after: the full after-image / payload keyed by the canonical column names,
        every value codec VARCHAR (str) or None; None on a 'd' event.
    """

    seq: int
    op: Literal["c", "u", "d", "join", "leave"]
    atom: "RecordAtom | MembershipAtom"
    record_id: str
    presentation_id: str | None
    event_sim_time: int
    ts: str | int
    after: dict[str, str | None] | None


def _build_record_after_image(
    row: "tuple[object, ...]",
    col_names: list[str],
    op: str,
    selected_properties: tuple[str, ...],
) -> dict[str, str | None] | None:
    """Build one record event's after-image, projected to selected properties.

    None on a 'd' event. Otherwise record_id, then presentation_id when the
    kind carries one (identity — unaffected by the properties projection),
    then one prop__<p> per selected property, in col_names order (the
    kind's sidecar declaration order, since selected_properties is always a
    subset of the full set the fold row was built from).

    Args:
        row: The fold output row.
        col_names: Column names parallel to the row tuple
            (record_fold_row_column_names' order, over the full property set).
        op: The event op ('c', 'u', or 'd').
        selected_properties: The resolved selection's effective property set.

    Returns:
        The projected after-image dict, or None on 'd'.
    """
    if op == "d":
        return None

    selected = frozenset(selected_properties)
    after: dict[str, str | None] = {"record_id": str(row[_IDX_RECORD_ID])}
    for idx in range(4, len(col_names)):
        name = col_names[idx]
        if name == "presentation_id":
            value = row[idx]
            after[name] = None if value is None else str(value)
            continue
        prop = name[len("prop__") :]
        if prop not in selected:
            continue
        value = row[idx]
        after[name] = None if value is None else str(value)
    return after


def _build_record_event_rows(
    emit: "Emit",
    fork_path: str,
    resolved_record: "ResolvedRecordSelection",
    anchor: "EffectiveAnchor | None",
) -> list[_EventRow]:
    """Materialize one record atom's fold rows into canonically-keyed events.

    Always invokes the fold over the kind's full fold-invocation property
    set (row set and seq independent of `properties`), then applies
    population restriction (sub_types, record_ids) as pure row selection and
    the `properties` projection on the after-image only.

    Args:
        emit: The open emit.
        fork_path: The sole branch's fork_path.
        resolved_record: One resolved RecordAtomSelection.
        anchor: The resolved effective anchor, or None for raw-ns ts.

    Returns:
        Canonically-keyed event rows for this atom, in the fold's own order
        (already the canonical order restricted to this one kind).
    """
    sidecar = emit.sidecar
    kind = resolved_record.kind
    full_properties = frozenset(resolved_record.full_properties)

    sql = build_row_state_events_sql(
        sidecar, fork_path, kind, full_properties, change_scope=full_properties
    )
    rows = emit.query(sql, ())
    col_names = record_fold_row_column_names(sidecar, kind, full_properties)

    is_subtyped = bool(sidecar.subtype_values(kind))
    discriminator_index = spine_discriminator_index(
        emit, fork_path, kind, is_subtyped, resolved_record.discriminator_declared
    )

    sub_types_filter = frozenset(resolved_record.sub_types)
    record_ids_filter = resolved_record.record_ids
    identity: tuple[str, ...] = (kind,)

    result: list[_EventRow] = []
    for row in rows:
        record_id = str(row[_IDX_RECORD_ID])
        sub_type = discriminator_index.get(record_id) if is_subtyped else None

        if sub_types_filter and sub_type not in sub_types_filter:
            continue
        if record_ids_filter is not None and record_id not in record_ids_filter:
            continue

        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        event_class = cast(int, row[_IDX_EVENT_CLASS])
        op = cast(Literal["c", "u", "d"], row[_IDX_OP])

        presentation_id: str | None = None
        if resolved_record.has_presentation_id:
            pid_idx = col_names.index("presentation_id")
            raw_pid = row[pid_idx]
            presentation_id = None if raw_pid is None else str(raw_pid)

        after = _build_record_after_image(
            row, col_names, op, resolved_record.properties
        )
        merge_key: _EventKey = (
            event_sim_time,
            event_class,
            _FAMILY_RECORD,
            identity,
            record_id,
        )

        result.append(
            (
                merge_key,
                op,
                RecordAtom(kind=kind, sub_type=sub_type),
                record_id,
                presentation_id,
                event_sim_time,
                render_ts(event_sim_time, anchor),
                after,
            )
        )

    return result


def _membership_field_name(column_name: str) -> str:
    """Recover the bare element-schema field name from a payload column.

    Args:
        column_name: 'elem__<f>', 'member__<f>__kind', or 'member__<f>__id'.

    Returns:
        The bare field name 'f'.
    """
    if column_name.startswith("member__") and column_name.endswith("__kind"):
        return column_name[len("member__") : -len("__kind")]
    if column_name.startswith("member__") and column_name.endswith("__id"):
        return column_name[len("member__") : -len("__id")]
    return column_name[len("elem__") :]


def _build_membership_after_image(
    row: "tuple[object, ...]",
    col_names: list[str],
    selected_fields: tuple[str, ...],
) -> dict[str, str | None]:
    """Build one membership event's after-image, projected to selected fields.

    Always non-None (both join and leave carry the full payload).

    Args:
        row: The fold output row.
        col_names: Column names parallel to the row tuple
            (membership_fold_row_column_names' order, over the full field set).
        selected_fields: The resolved selection's effective field set.

    Returns:
        The projected after-image dict.
    """
    selected = frozenset(selected_fields)
    after: dict[str, str | None] = {"record_id": str(row[_IDX_RECORD_ID])}
    for idx in range(4, len(col_names)):
        name = col_names[idx]
        if _membership_field_name(name) not in selected:
            continue
        value = row[idx]
        after[name] = None if value is None else str(value)
    return after


def _build_membership_event_rows(
    emit: "Emit",
    fork_path: str,
    resolved_membership: "ResolvedMembershipSelection",
    anchor: "EffectiveAnchor | None",
) -> list[_EventRow]:
    """Materialize one membership atom's fold rows into canonically-keyed events.

    Always invokes the fold over the table's full element-schema field set
    (the declared ORDER BY field tail, and therefore seq, independent of
    `fields`), then applies population restriction (owner_sub_types,
    owner_record_ids) as pure row selection and the `fields` projection on
    the after-image only.

    Args:
        emit: The open emit.
        fork_path: The sole branch's fork_path.
        resolved_membership: One resolved MembershipAtomSelection.
        anchor: The resolved effective anchor, or None for raw-ns ts.

    Returns:
        Canonically-keyed event rows for this atom, in the fold's own order.
    """
    sidecar = emit.sidecar
    owner_kind = resolved_membership.owner_kind
    property_name = resolved_membership.property_name
    full_fields = resolved_membership.full_fields

    sql = build_membership_events_sql(
        sidecar, fork_path, owner_kind, property_name, full_fields
    )
    rows = emit.query(sql, ())
    col_names = membership_fold_row_column_names(
        sidecar, owner_kind, property_name, full_fields
    )

    is_owner_subtyped = bool(sidecar.subtype_values(owner_kind))
    discriminator_index = spine_discriminator_index(
        emit,
        fork_path,
        owner_kind,
        is_owner_subtyped,
        resolved_membership.owner_discriminator_declared,
    )

    owner_sub_types_filter = frozenset(resolved_membership.owner_sub_types)
    owner_record_ids_filter = resolved_membership.owner_record_ids
    identity: tuple[str, ...] = (owner_kind, property_name)

    result: list[_EventRow] = []
    for row in rows:
        record_id = str(row[_IDX_RECORD_ID])
        owner_sub_type = (
            discriminator_index.get(record_id) if is_owner_subtyped else None
        )

        if owner_sub_types_filter and owner_sub_type not in owner_sub_types_filter:
            continue
        if (
            owner_record_ids_filter is not None
            and record_id not in owner_record_ids_filter
        ):
            continue

        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        event_class = cast(int, row[_IDX_EVENT_CLASS])
        op = cast(Literal["join", "leave"], row[_IDX_OP])

        after = _build_membership_after_image(
            row, col_names, resolved_membership.fields
        )
        merge_key: _EventKey = (
            event_sim_time,
            event_class,
            _FAMILY_MEMBERSHIP,
            identity,
            record_id,
        )

        result.append(
            (
                merge_key,
                op,
                MembershipAtom(
                    owner_kind=owner_kind,
                    owner_sub_type=owner_sub_type,
                    property_name=property_name,
                ),
                record_id,
                None,
                event_sim_time,
                render_ts(event_sim_time, anchor),
                after,
            )
        )

    return result


def iter_playback_events(
    emit: "Emit",
    resolved: "ResolvedSelection",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    start_sim_time: int | None,
    end_sim_time: int | None,
) -> Iterator[PlaybackEvent]:
    """Yield in-scope PlaybackEvents in canonical order, seq entry-point-invariant.

    A generator function: no fold is queried until the first `next()` (the
    pull commitment). Every selected atom's fold rows are k-way merged under
    the canonical key; `seq` is assigned over the whole merged stream before
    the [start_sim_time, end_sim_time) bound is applied, so a head opened at
    any lower bound continues the same numbering.

    Args:
        emit: The open emit.
        resolved: The selection resolved against the sidecar at open.
        anchor: The resolved effective anchor, or None for raw-ns ts.
        fork_path: The sole branch's fork_path.
        start_sim_time: Inclusive lower bound (ns), or None for tape start.
        end_sim_time: Exclusive upper bound (ns), or None for tape end.

    Yields:
        PlaybackEvents with event_sim_time in [start_sim_time, end_sim_time).
    """
    streams: list[list[_EventRow]] = [
        _build_record_event_rows(emit, fork_path, record_sel, anchor)
        for record_sel in resolved.records
    ] + [
        _build_membership_event_rows(emit, fork_path, membership_sel, anchor)
        for membership_sel in resolved.memberships
    ]

    seq = 0
    merged = heapq.merge(*streams, key=lambda row: row[0])
    for (
        _merge_key,
        op,
        atom,
        record_id,
        presentation_id,
        event_sim_time,
        ts,
        after,
    ) in merged:
        seq += 1
        if start_sim_time is not None and event_sim_time < start_sim_time:
            continue
        if end_sim_time is not None and event_sim_time >= end_sim_time:
            continue
        yield PlaybackEvent(
            seq=seq,
            op=op,
            atom=atom,
            record_id=record_id,
            presentation_id=presentation_id,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
        )
