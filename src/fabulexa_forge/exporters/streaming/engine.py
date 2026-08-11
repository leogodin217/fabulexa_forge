"""Streaming engine: validation pass, per-stream fold materialization, k-way merge.

Produces StreamEvent objects in the canonical total order with seq stamped and
ts rendered from the EffectiveAnchor. Materializes one fold per declared
stream (not per kind/table): a kind-shaped stream's event set is
payload-independent (change scope = the kind's full property set, projection
= the stream's declared properties), and every stream's rows merge under a
canonical key whose source-identity component is the declared stream name.
Layer-direction invariant: imports derivations, config, reader, anchor, and
errors — never writers or CLI.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Iterator, Sequence, cast

from fabulexa_forge.anchor import render_ts
from fabulexa_forge.config.models import KindStream, MembershipStream
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.membership_events import (
    build_membership_events_sql,
    field_resolves,
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
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.exporters.streaming.routing import (
    membership_route_attributes,
    resolve_subtype_index,
    route_attributes,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.errors import TableNotFoundError

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import StreamConfig
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

# Indices into the fold output tuple (state-changes and membership-events)
_IDX_RECORD_ID = 0
_IDX_EVENT_SIM_TIME = 1
_IDX_EVENT_CLASS = 2
_IDX_OP = 3

# The canonical merge key: (event_sim_time, event_class, stream_name, record_id).
_MergeKey = tuple[int, int, str, str]
_MergeRow = tuple[_MergeKey, tuple[object, ...], str]


def _check_stream_properties_slice_only(
    sidecar: "Sidecar",
    name: str,
    kind: str,
    properties: Sequence[str],
) -> None:
    """Enforce StreamPropertySliceOnly over one kind-shaped stream's properties.

    No `properties` entry may resolve to a non-exempt slice_only prop__<p>
    column of records__<kind>. Refuse-only; emits nothing.

    Args:
        sidecar: The open emit's sidecar.
        name: The declaring stream's name.
        kind: The record kind owning the selected properties.
        properties: The selected property names (bare, prop__ stripped).

    Raises:
        ExportError: A selected property resolves to a non-exempt slice_only
            column. Message leads with the stream name.
        TemporalClassUnavailableError: Propagated.
    """
    for prop in properties:
        column_name = f"prop__{prop}"
        if is_non_exempt_slice_only(sidecar, kind, column_name):
            raise ExportError(
                f"stream '{name}': stream kind '{kind}': property '{prop}'"
                " is temporal_class: slice_only; it cannot ride the"
                " state-changes after-image"
            )


def _validate_kind_stream(sidecar: "Sidecar", stream: "KindStream") -> None:
    """Run StreamKindResolvable through StreamPropertySliceOnly for one stream.

    Args:
        sidecar: The open emit's sidecar view.
        stream: The kind-shaped stream declaration.

    Raises:
        ExportError: StreamKindResolvable, StreamSubTypesRequireSubtyping,
            StreamSubTypesDeclared, StreamPropertyResolvable, or
            StreamPropertySliceOnly fails. Message leads with the stream name.
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    kind = stream.kind
    table_name = f"records__{kind}"
    try:
        cols = sidecar.columns(table_name)
    except TableNotFoundError:
        raise ExportError(
            f"stream '{stream.name}': kind '{kind}' has no records__{kind} table"
        ) from None

    declared = sidecar.subtype_values(kind)
    is_subtyped = bool(declared)

    if stream.sub_types is not None:
        if not is_subtyped:
            raise ExportError(
                f"stream '{stream.name}': kind '{kind}' is not sub-typed;"
                " sub_types is not addressable"
            )
        declared_set = set(declared)
        for value in stream.sub_types:
            if value not in declared_set:
                raise ExportError(
                    f"stream '{stream.name}': sub_type '{value}' is not declared"
                    f" for kind '{kind}'"
                )

    sidecar_prop_names = {
        c.name[len("prop__") :] for c in cols if c.name.startswith("prop__")
    }
    for prop in stream.properties:
        if prop not in sidecar_prop_names:
            raise ExportError(
                f"stream '{stream.name}': property '{prop}'"
                f" has no prop__{prop} column on kind '{kind}'"
            )

    _check_stream_properties_slice_only(sidecar, stream.name, kind, stream.properties)


def _validate_membership_stream(sidecar: "Sidecar", stream: "MembershipStream") -> None:
    """Run MembershipResolvable and MembershipFieldResolvable for one stream.

    Args:
        sidecar: The open emit's sidecar view.
        stream: The membership-shaped stream declaration.

    Raises:
        ExportError: MembershipResolvable or MembershipFieldResolvable fails.
            Message leads with the stream name.
    """
    kind = stream.membership.kind
    property_name = stream.membership.property
    table_name = f"membership__{kind}__{property_name}"
    try:
        cols = sidecar.columns(table_name)
    except TableNotFoundError:
        raise ExportError(
            f"stream '{stream.name}': membership '{kind}.{property_name}'"
            f" has no {table_name} table"
        ) from None

    if stream.fields:
        col_names = {c.name for c in cols}
        for field in stream.fields:
            if not field_resolves(col_names, field):
                raise ExportError(
                    f"stream '{stream.name}': field '{field}'"
                    " has no elem__/member__ column"
                )


def _validate_streams(emit: "Emit", config: "StreamConfig") -> str:
    """Run the eager business-rule validation pass over every declared stream.

    Checks the single-branch guard, then each stream's rules: kind-shaped
    streams run StreamKindResolvable / StreamSubTypesRequireSubtyping /
    StreamSubTypesDeclared / StreamPropertyResolvable / StreamPropertySliceOnly;
    membership-shaped streams run MembershipResolvable /
    MembershipFieldResolvable.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.

    Returns:
        The fork_path (from require_single_branch).

    Raises:
        ExportError: The single-branch guard fails, or any per-stream business
            rule fails — message leading with the offending stream's name.
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    fork_path = require_single_branch(emit.sidecar)
    sidecar = emit.sidecar
    for stream in config.streams:
        if isinstance(stream, KindStream):
            _validate_kind_stream(sidecar, stream)
        else:
            _validate_membership_stream(sidecar, stream)
    return fork_path


def _is_kind_subtyped(kind: str, sidecar: "Sidecar") -> bool:
    """Whether kind is sub-typed per the sidecar's discriminator domain.

    Delegates to ``Sidecar.subtype_values`` — a kind is sub-typed iff its
    ``<kind>_type`` discriminator domain is non-empty.

    Args:
        kind: The record kind.
        sidecar: The open emit's sidecar view.

    Returns:
        True iff ``sidecar.subtype_values(kind)`` is non-empty.
    """
    return bool(sidecar.subtype_values(kind))


def _kind_property_names(sidecar: "Sidecar", kind: str) -> frozenset[str]:
    """Every bare property name declared on kind's records table.

    The change-scope set for a kind-shaped stream's fold (design doc § Per-
    stream folds and after-images): the event set is a fact of the
    population, independent of the stream's own `properties` selection. Read
    directly off the sidecar — a full-population fact, not an author-declared
    value.

    Args:
        sidecar: The open emit's sidecar view.
        kind: The record kind.

    Returns:
        Every prop__<p> column's bare name on records__<kind>.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
    """
    cols = sidecar.columns(f"records__{kind}")
    return frozenset(
        col.name[len("prop__") :] for col in cols if col.name.startswith("prop__")
    )


def _build_subtype_indexes(
    emit: "Emit",
    config: "StreamConfig",
    sidecar: "Sidecar",
) -> dict[str, dict[str, str]]:
    """Build per-sub-typed-kind record_id -> sub_type indexes.

    One index per distinct kind referenced by a kind-shaped stream that is
    sub-typed per ``Sidecar.subtype_values``.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        sidecar: The open emit's sidecar view.

    Returns:
        A mapping kind -> {record_id -> sub_type} for sub-typed kinds only.
    """
    indexes: dict[str, dict[str, str]] = {}
    for stream in config.streams:
        if isinstance(stream, KindStream) and stream.kind not in indexes:
            if _is_kind_subtyped(stream.kind, sidecar):
                indexes[stream.kind] = resolve_subtype_index(emit, stream.kind)
    return indexes


def _filter_rows_by_types(
    rows: list[tuple[object, ...]],
    selected_types: list[str],
    subtype_index: dict[str, str],
) -> list[tuple[object, ...]]:
    """Filter fold rows to only those matching selected sub-types.

    Args:
        rows: Materialized fold rows for one kind-shaped stream.
        selected_types: The stream's declared sub_types (non-empty).
        subtype_index: The kind's record_id -> sub_type index.

    Returns:
        Only the rows whose record's sub_type is in selected_types.
    """
    selected_set = frozenset(selected_types)
    return [
        row
        for row in rows
        if subtype_index.get(str(row[_IDX_RECORD_ID])) in selected_set
    ]


def _rows_to_keyed(
    rows: list[tuple[object, ...]],
    stream_name: str,
) -> list[_MergeRow]:
    """Pair each fold row with its canonical merge key and declaring stream name.

    The canonical total order is (event_sim_time, event_class, stream_name,
    record_id) — the source-identity component is the declared stream name
    (design doc § Merge order): unique by the stream_names_unique validator,
    so the inter-stream tiebreak stays deterministic.

    Args:
        rows: Materialized fold rows for one stream.
        stream_name: The declaring stream's name.

    Returns:
        A list of (merge_key, row, stream_name) tuples, in the same order as rows.
    """
    keyed: list[_MergeRow] = []
    for row in rows:
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        event_class = cast(int, row[_IDX_EVENT_CLASS])
        record_id = cast(str, row[_IDX_RECORD_ID])
        merge_key = (event_sim_time, event_class, stream_name, record_id)
        keyed.append((merge_key, row, stream_name))
    return keyed


def _build_after_image(
    row: tuple[object, ...],
    col_names: list[str],
    op: str,
) -> dict[str, object] | None:
    """Build the after-image dict for one row-state-events row.

    On a delete op the after-image is None. Otherwise, builds a dict from
    every column after the 4-column fixed prefix (record_id, event_sim_time,
    event_class, op), which gives: presentation_id (when present) and all
    prop__<p> columns. Also adds record_id as the first after-image key.

    Args:
        row: The fold output row.
        col_names: Column names parallel to the row tuple.
        op: The op string ('c', 'u', or 'd').

    Returns:
        A dict[str, object] (str-or-null values) for c/u, or None for d.
    """
    if op == "d":
        return None

    after: dict[str, object] = {}
    after["record_id"] = row[_IDX_RECORD_ID]
    for i in range(4, len(col_names)):
        after[col_names[i]] = row[i]
    return after


def _build_membership_after_image(
    row: tuple[object, ...],
    col_names: list[str],
) -> dict[str, object]:
    """Build the after-image dict for one membership event row.

    Membership events always have a non-null after-image (both join and leave).
    The after-image includes record_id and the payload field columns (elem__/member__).

    Args:
        row: The membership fold output row.
        col_names: Column names parallel to the row tuple.

    Returns:
        A dict[str, object] (str-or-null values) with record_id first, then
        payload field columns in resolve_membership_columns order.
    """
    after: dict[str, object] = {}
    after["record_id"] = row[_IDX_RECORD_ID]
    for i in range(4, len(col_names)):
        after[col_names[i]] = row[i]
    return after


def _iter_kind_streams_inner(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    sidecar: "Sidecar",
    subtype_indexes: dict[str, dict[str, str]],
) -> Iterator[StreamEvent]:
    """Materialize one fold per kind-shaped stream, merge, and yield StreamEvents.

    Called only after the eager validation pass in iter_stream_events has
    succeeded, for content='state-changes'. Each stream's fold runs with
    change_scope = the kind's full property set (payload-independent event
    set) and projection = the stream's declared properties; a stream that
    scopes an explicit sub_types subset is filtered post-fold via the
    discriminator index.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).
        sidecar: The open emit's sidecar view.
        subtype_indexes: Per-sub-typed-kind record_id -> sub_type indexes.

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    stream_rows: list[list[_MergeRow]] = []
    col_names_by_stream: dict[str, list[str]] = {}
    kind_by_stream: dict[str, str] = {}

    for stream in config.streams:
        assert isinstance(stream, KindStream)
        kind = stream.kind
        properties = frozenset(stream.properties)
        change_scope = _kind_property_names(sidecar, kind)

        sql = build_row_state_events_sql(
            sidecar, fork_path, kind, properties, change_scope=change_scope
        )
        rows = emit.query(sql, ())

        if stream.sub_types is not None and kind in subtype_indexes:
            rows = _filter_rows_by_types(rows, stream.sub_types, subtype_indexes[kind])

        stream_rows.append(_rows_to_keyed(rows, stream.name))
        col_names_by_stream[stream.name] = record_fold_row_column_names(
            sidecar, kind, properties
        )
        kind_by_stream[stream.name] = kind

    seq = 0
    for _merge_key, row, stream_name in heapq.merge(*stream_rows, key=lambda x: x[0]):
        seq += 1

        op = str(row[_IDX_OP])
        record_id = str(row[_IDX_RECORD_ID])
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        kind = kind_by_stream[stream_name]

        col_names = col_names_by_stream[stream_name]
        has_pid = "presentation_id" in col_names

        presentation_id: str | None = None
        if has_pid:
            pid_idx = col_names.index("presentation_id")
            raw_pid = row[pid_idx]
            presentation_id = str(raw_pid) if raw_pid is not None else None

        after = _build_after_image(row, col_names, op)
        ts = render_ts(event_sim_time, anchor)

        sub_type: str | None = None
        kind_is_subtyped = _is_kind_subtyped(kind, sidecar)
        if kind_is_subtyped and kind in subtype_indexes:
            sub_type = subtype_indexes[kind].get(record_id)
        attrs = route_attributes(kind_is_subtyped, kind, sub_type)

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=kind,
            record_id=record_id,
            presentation_id=presentation_id,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=stream_name,
            route_table=attrs["route_table"],
        )


def _iter_membership_streams_inner(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
) -> Iterator[StreamEvent]:
    """Materialize one fold per membership-shaped stream, merge, and yield events.

    Called only after the eager validation pass in iter_stream_events has
    succeeded, for content='membership-events'.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    stream_rows: list[list[_MergeRow]] = []
    col_names_by_stream: dict[str, list[str]] = {}
    owner_kind_by_stream: dict[str, str] = {}
    property_by_stream: dict[str, str] = {}

    for stream in config.streams:
        assert isinstance(stream, MembershipStream)
        owner_kind = stream.membership.kind
        property_name = stream.membership.property

        sql = build_membership_events_sql(
            emit.sidecar, fork_path, owner_kind, property_name, stream.fields
        )
        rows = emit.query(sql, ())

        stream_rows.append(_rows_to_keyed(rows, stream.name))
        col_names_by_stream[stream.name] = membership_fold_row_column_names(
            emit.sidecar, owner_kind, property_name, stream.fields
        )
        owner_kind_by_stream[stream.name] = owner_kind
        property_by_stream[stream.name] = property_name

    seq = 0
    for _merge_key, row, stream_name in heapq.merge(*stream_rows, key=lambda x: x[0]):
        seq += 1

        op = str(row[_IDX_OP])
        record_id = str(row[_IDX_RECORD_ID])
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        owner_kind = owner_kind_by_stream[stream_name]
        property_name = property_by_stream[stream_name]

        col_names = col_names_by_stream[stream_name]
        after = _build_membership_after_image(row, col_names)
        ts = render_ts(event_sim_time, anchor)

        attrs = membership_route_attributes(owner_kind, property_name)

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=owner_kind,
            record_id=record_id,
            presentation_id=None,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=stream_name,
            route_table=attrs["route_table"],
        )


def iter_stream_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
) -> Iterator[StreamEvent]:
    """Yield the stream's events in canonical total order with seq stamped.

    Split into two phases: an eager validation pass that runs at call time
    (before the first next()), then an inner generator that materializes one
    fold per declared stream (kind-shaped: change scope = the kind's full
    property set, projection = the stream's declared properties;
    membership-shaped: unchanged), drops rows outside the stream's sub_types
    scope post-fold via the discriminator index, k-way-merges under
    (event_sim_time, event_class, stream_name, record_id), stamps seq, renders
    ts, stamps topic = the declaring stream's name and route_table = the
    per-event leaf, and yields StreamEvents.

    See § Ordering and `seq`, § Timestamp rendering, § Business Rules.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None.

    Returns:
        An iterator of StreamEvent in global seq order.

    Raises:
        ExportError: An eager business rule failed — message leads with the
            offending stream's name. Raised at call time, before the first
            next().
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    fork_path = _validate_streams(emit, config)

    if config.content == "membership-events":
        return _iter_membership_streams_inner(emit, config, anchor, fork_path)

    subtype_indexes = _build_subtype_indexes(emit, config, emit.sidecar)
    return _iter_kind_streams_inner(
        emit, config, anchor, fork_path, emit.sidecar, subtype_indexes
    )


def build_topic_set(config: "StreamConfig") -> tuple[str, ...]:
    """The run's topic set: the declared stream names, in declaration order.

    Pure function of the config — declared intent, not observed rows, drives
    topic existence (the declared-but-empty guarantee is keyed on this set).
    Names are unique by the stream_names_unique validator, so the tuple is
    duplicate-free by construction.

    Args:
        config: The validated streaming configuration.

    Returns:
        Each stream's `name`, config order.
    """
    return tuple(stream.name for stream in config.streams)
