"""Streaming engine: validation pass, per-kind fold materialization, k-way merge.

Produces StreamEvent objects in the canonical total order with seq stamped and
ts rendered from the EffectiveAnchor. Layer-direction invariant: imports
derivations, config, reader, anchor, and errors — never writers or CLI.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Iterator, Sequence, cast

from fabulexa_forge.anchor import render_ts
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.membership_events import (
    build_membership_events_sql,
    resolve_membership_columns,
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
    enumerate_topics,
    membership_route_attributes,
    resolve_subtype_index,
    resolve_topic,
    route_attributes,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.errors import TableNotFoundError

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import RoutingConfig, StreamConfig
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

# Indices into the fold output tuple (state-changes and membership-events)
_IDX_RECORD_ID = 0
_IDX_EVENT_SIM_TIME = 1
_IDX_EVENT_CLASS = 2
_IDX_OP = 3


def _default_routing() -> "RoutingConfig":
    """Return the default (no-routing-block) RoutingConfig.

    Imported lazily to avoid a circular import at module load time.

    Returns:
        A RoutingConfig with topic_template='{route_table}', groups={},
        table_identity='source_table'.
    """
    from fabulexa_forge.config.models import RoutingConfig

    return RoutingConfig()


def _validate_group_members_resolve(
    routing: "RoutingConfig",
    rendered_base_topics: set[str],
) -> None:
    """Check StreamGroupMembersResolve: every groups member matches a rendered topic.

    Args:
        routing: The effective routing policy.
        rendered_base_topics: Set of base topic names rendered for the current content.

    Raises:
        ExportError: A groups member does not appear in rendered_base_topics.
    """
    for target, members in routing.groups.items():
        for member in members:
            if member not in rendered_base_topics:
                raise ExportError(
                    f"routing.groups member '{member}'"
                    f" matches no streamed route (target '{target}')"
                )


def _validate_routing_rules(
    emit: "Emit",
    config: "StreamConfig",
    routing: "RoutingConfig",
    sidecar: "Sidecar",
) -> None:
    """Validate the routing business rules against the emit's sidecar.

    Rules checked:
    - StreamTypesRequireSubtyping
    - StreamTypesDeclared
    - StreamTemplatePlaceholders
    - StreamGroupMembersResolve

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        routing: The effective routing policy.
        sidecar: The open emit's sidecar view.

    Raises:
        ExportError: Any routing business rule fails.
    """
    selected_attributes: list[dict[str, str]] = []
    # Stores (kind, is_subtyped, placeholder_sub_types) for pass 2.
    # sidecar.subtype_values(kind) is called exactly once per kind in pass 1.
    kind_pass2: list[tuple[str, bool, list[str | None]]] = []

    # Pass 1: StreamTypesRequireSubtyping + StreamTypesDeclared for ALL kinds
    for kind_sel in config.kinds:
        kind = kind_sel.kind
        declared = sidecar.subtype_values(kind)
        is_subtyped = bool(declared)

        # StreamTypesRequireSubtyping
        if kind_sel.types and not is_subtyped:
            raise ExportError(
                f"kind '{kind}' is not sub-typed;"
                f" remove 'types' (sub-type selection requires a sub-typed kind)"
            )

        placeholder_sub_types: list[str | None]
        if is_subtyped:
            # StreamTypesDeclared
            declared_set = set(declared)
            for value in kind_sel.types:
                if value not in declared_set:
                    raise ExportError(
                        f"kind '{kind}' has no sub-type '{value}';"
                        f" declared sub-types are {list(declared)}"
                    )

            # Selected sub-types: either the explicit list or all declared
            emit_sub_types = kind_sel.types if kind_sel.types else list(declared)
            for sub_type in emit_sub_types:
                attrs = route_attributes(True, kind, sub_type)
                selected_attributes.append(attrs)
            placeholder_sub_types = list(emit_sub_types)
        else:
            attrs = route_attributes(False, kind, None)
            selected_attributes.append(attrs)
            placeholder_sub_types = [None]

        kind_pass2.append((kind, is_subtyped, placeholder_sub_types))

    # Pass 2: StreamTemplatePlaceholders for ALL kinds
    for kind, is_subtyped, placeholder_sub_types in kind_pass2:
        for check_sub_type in placeholder_sub_types:
            attrs = route_attributes(is_subtyped, kind, check_sub_type)
            try:
                routing.topic_template.format(**attrs)
            except KeyError as exc:
                placeholder = str(exc).strip("'\"")
                raise ExportError(
                    f"topic_template references '{placeholder}',"
                    f" absent for non-sub-typed kind '{kind}'"
                ) from exc

    # StreamGroupMembersResolve — every groups member must match a rendered base topic
    rendered_base_topics: set[str] = set()
    for attrs in selected_attributes:
        base_name = routing.topic_template.format(**attrs)
        rendered_base_topics.add(base_name)

    _validate_group_members_resolve(routing, rendered_base_topics)


def _validate_membership_routing_rules(
    config: "StreamConfig",
    routing: "RoutingConfig",
) -> None:
    """Validate routing business rules for membership-events content.

    Checks StreamTemplatePlaceholders and StreamGroupMembersResolve for
    membership route attributes (owner_kind, property, route_table; no sub_type).

    Args:
        config: The validated streaming configuration.
        routing: The effective routing policy.

    Raises:
        ExportError: StreamTemplatePlaceholders (template references unknown key) or
            StreamGroupMembersResolve (group member matches no rendered base topic).
    """
    rendered_base_topics: set[str] = set()

    for ms in config.memberships:
        attrs = membership_route_attributes(ms.owner_kind, ms.property)
        try:
            base_name = routing.topic_template.format(**attrs)
        except KeyError as exc:
            placeholder = str(exc).strip("'\"")
            raise ExportError(
                f"topic_template references '{placeholder}',"
                f" absent for membership table"
                f" '{ms.owner_kind}__{ms.property}'"
            ) from exc
        rendered_base_topics.add(base_name)

    _validate_group_members_resolve(routing, rendered_base_topics)


def _validate_memberships(
    emit: "Emit",
    config: "StreamConfig",
    routing: "RoutingConfig",
) -> str:
    """Run the eager validation pass for membership-events content.

    Checks: single-branch guard; for each membership selection that
    membership__<owner_kind>__<property> resolves in the sidecar
    (MembershipResolvable); for each selected field that it resolves to
    elem__/member__ columns on that table (MembershipFieldResolvable); then
    routing business rules.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        routing: The effective routing policy.

    Returns:
        The fork_path (from require_single_branch).

    Raises:
        ExportError: The single-branch guard fails, a membership table is absent
            (MembershipResolvable), a selected field has no elem__/member__ column
            (MembershipFieldResolvable), or a routing business rule fails. Raised
            at call time, before the first next().
    """
    fork_path = require_single_branch(emit.sidecar)

    for ms in config.memberships:
        table_name = f"membership__{ms.owner_kind}__{ms.property}"
        try:
            # MembershipResolvable — table must exist in sidecar
            emit.sidecar.columns(table_name)
        except TableNotFoundError:
            raise ExportError(
                f"membership table '{table_name}' not found in the emit"
            ) from None

        if ms.fields:
            # MembershipFieldResolvable — fields must resolve to elem__/member__ columns
            resolve_membership_columns(
                emit.sidecar, ms.owner_kind, ms.property, ms.fields
            )

    _validate_membership_routing_rules(config, routing)

    return fork_path


def _validate_kinds(
    emit: "Emit",
    config: "StreamConfig",
    routing: "RoutingConfig",
) -> str:
    """Run the up-front business-rule validation pass.

    Checks: single-branch guard, then for each kind that records__<kind>
    resolves in the sidecar (StreamKindResolvable), each selected property
    that its prop__<p> column resolves (StreamPropertyResolvable), and each
    selected property does not resolve to a non-exempt slice_only column
    (StreamPropertySliceOnly). Then runs the routing business rules.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        routing: The effective routing policy.

    Returns:
        The fork_path (from require_single_branch).

    Raises:
        ExportError: The single-branch guard fails, a kind has no
            records__<kind> table, a property has no prop__<property> column,
            a selected property resolves to a non-exempt slice_only column,
            or a routing business rule fails.
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    # SingleBranch guard — raises ExportError with verbatim message
    fork_path = require_single_branch(emit.sidecar)

    for kind_sel in config.kinds:
        kind = kind_sel.kind
        table_name = f"records__{kind}"

        # StreamKindResolvable
        try:
            cols = emit.sidecar.columns(table_name)
        except TableNotFoundError:
            raise ExportError(
                f"stream kind '{kind}' has no records__{kind} table"
            ) from None

        # StreamPropertyResolvable — check each selected property
        sidecar_prop_names = {
            col.name[len("prop__") :] for col in cols if col.name.startswith("prop__")
        }
        for prop in kind_sel.properties:
            if prop not in sidecar_prop_names:
                raise ExportError(
                    f"stream kind '{kind}': property '{prop}'"
                    f" has no prop__{prop} column"
                )

        # StreamPropertySliceOnly
        _check_stream_properties_slice_only(emit.sidecar, kind, kind_sel.properties)

    # Routing business rules
    _validate_routing_rules(emit, config, routing, emit.sidecar)

    return fork_path


def _check_stream_properties_slice_only(
    sidecar: "Sidecar",
    kind: str,
    properties: Sequence[str],
) -> None:
    """Enforce StreamPropertySliceOnly over one kind's selected properties.

    No kinds[].properties entry may resolve to a non-exempt slice_only
    prop__<p> column of records__<kind>. Hooked in _validate_kinds' per-kind
    loop immediately after the existing property-resolvability check (column
    existence already established). Refuse-only; emits nothing.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind owning the selected properties.
        properties: The selected property names (bare, prop__ stripped).

    Raises:
        ExportError: A selected property resolves to a non-exempt slice_only
            column. Message names the kind, the property, and the class.
        TemporalClassUnavailableError: Propagated.
    """
    for prop in properties:
        column_name = f"prop__{prop}"
        if is_non_exempt_slice_only(sidecar, kind, column_name):
            raise ExportError(
                f"stream kind '{kind}': property '{prop}' is temporal_class:"
                " slice_only; it cannot ride the state-changes after-image"
            )


def _build_merge_key(
    row: tuple[object, ...],
    source_identity: str,
) -> tuple[int, int, str, str]:
    """Extract the canonical merge key for a fold row with source_identity injected.

    The canonical total order is
    (event_sim_time, event_class, source_identity, record_id).
    For state-changes, source_identity is the kind. For membership-events,
    source_identity is "<owner_kind>__<property>" (the route_table value).
    Since source_identity is not a fold column it is injected per-stream here.

    Args:
        row: A fold output row tuple.
        source_identity: The source identity string for this stream.

    Returns:
        The 4-tuple merge key.
    """
    event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
    event_class = cast(int, row[_IDX_EVENT_CLASS])
    record_id = cast(str, row[_IDX_RECORD_ID])
    return (event_sim_time, event_class, source_identity, record_id)


def _rows_to_keyed(
    rows: list[tuple[object, ...]],
    source_identity: str,
) -> list[tuple[tuple[int, int, str, str], tuple[object, ...], str]]:
    """Pair each fold row with its canonical merge key and source_identity.

    Args:
        rows: Materialized fold rows for one stream.
        source_identity: The source identity string for this stream.

    Returns:
        A list of (merge_key, row, source_identity) tuples, in the same order as rows.
    """
    return [
        (_build_merge_key(row, source_identity), row, source_identity) for row in rows
    ]


def _build_after_image(
    row: tuple[object, ...],
    col_names: list[str],
    op: str,
) -> dict[str, object] | None:
    """Build the after-image dict for one event row.

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
    # The after-image includes record_id and every column after the 4-col prefix
    # col_names[0] = record_id, col_names[1] = event_sim_time,
    # col_names[2] = event_class, col_names[3] = op
    # col_names[4+] = presentation_id (optional), then prop__* columns
    after["record_id"] = row[_IDX_RECORD_ID]
    for i in range(4, len(col_names)):
        after[col_names[i]] = row[i]
    return after


def _build_subtype_indexes(
    emit: "Emit",
    config: "StreamConfig",
    sidecar: "Sidecar",
) -> dict[str, dict[str, str]]:
    """Build per-sub-typed-kind record_id -> sub_type indexes.

    Only builds indexes for kinds that are sub-typed per ``Sidecar.subtype_values``.

    Args:
        emit: The open emit.
        config: The validated streaming configuration.
        sidecar: The open emit's sidecar view.

    Returns:
        A mapping kind -> {record_id -> sub_type} for sub-typed kinds only.
    """
    indexes: dict[str, dict[str, str]] = {}

    for kind_sel in config.kinds:
        kind = kind_sel.kind
        if _is_kind_subtyped(kind, sidecar):
            indexes[kind] = resolve_subtype_index(emit, kind)

    return indexes


def _filter_rows_by_types(
    rows: list[tuple[object, ...]],
    kind: str,
    selected_types: list[str],
    subtype_index: dict[str, str],
) -> list[tuple[object, ...]]:
    """Filter fold rows to only those matching selected sub-types.

    Args:
        rows: Materialized fold rows for one kind.
        kind: The record kind.
        selected_types: The selected sub-type values (non-empty).
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


def _selected_attributes_for_kind(
    kind: str,
    kind_types: list[str],
    sidecar: "Sidecar",
) -> list[dict[str, str]]:
    """Build all possible route-attribute mappings for one kind in the run.

    For sub-typed kinds (non-empty ``Sidecar.subtype_values(kind)``), one
    mapping per selected sub-type — ``kind_types`` when the author scoped a
    subset, else the full declared domain. For a non-sub-typed kind, a single
    mapping for the kind. The declared sub-type set and its order come from the
    ``<kind>_type`` discriminator domain, never from ``record_roles``.

    Args:
        kind: The record kind.
        kind_types: Selected sub-type values; empty means all declared.
        sidecar: The open emit's sidecar view.

    Returns:
        Ordered list of route-attribute mappings for enumeration.
    """
    declared = sidecar.subtype_values(kind)
    is_subtyped = bool(declared)
    if is_subtyped:
        sub_types = kind_types if kind_types else list(declared)
        return [route_attributes(True, kind, st) for st in sub_types]
    return [route_attributes(False, kind, None)]


def selected_attributes_for_kind(
    kind: str,
    kind_types: list[str],
    sidecar: "Sidecar",
) -> list[dict[str, str]]:
    """Build all route-attribute mappings for one selected kind.

    For a sub-typed kind (non-empty ``Sidecar.subtype_values(kind)``), one
    mapping per selected sub-type — ``kind_types`` when the author scoped a
    subset, else the full declared domain. For a non-sub-typed kind, a single
    mapping for the kind. The declared sub-type set and its order come from the
    ``<kind>_type`` discriminator domain, never from ``record_roles``.

    Args:
        kind: The record kind.
        kind_types: Selected sub-type values; empty means all declared.
        sidecar: The open emit's sidecar view.

    Returns:
        Ordered list of route-attribute mappings for enumeration.
    """
    return _selected_attributes_for_kind(kind, kind_types, sidecar)


def _build_all_selected_attributes(
    config: "StreamConfig",
    sidecar: "Sidecar",
) -> list[dict[str, str]]:
    """Build all selected route-attribute mappings for the full run.

    Dispatches on config.content: for 'state-changes' iterates config.kinds; for
    'membership-events' iterates config.memberships (one mapping per table via
    membership_route_attributes, no per-kind sub-type fan-out).

    Args:
        config: The validated streaming configuration.
        sidecar: The open emit's sidecar view.

    Returns:
        Ordered list of all selected route-attribute mappings.
    """
    if config.content == "membership-events":
        return [
            membership_route_attributes(ms.owner_kind, ms.property)
            for ms in config.memberships
        ]
    all_attrs: list[dict[str, str]] = []
    for kind_sel in config.kinds:
        all_attrs.extend(
            _selected_attributes_for_kind(kind_sel.kind, kind_sel.types, sidecar)
        )
    return all_attrs


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
    # col_names[4+] are the payload field columns (elem__/member__ columns)
    for i in range(4, len(col_names)):
        after[col_names[i]] = row[i]
    return after


def _iter_membership_events_inner(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    routing: "RoutingConfig",
) -> Iterator[StreamEvent]:
    """Materialize membership folds, merge, stamp seq/topic/route_table, yield events.

    Called only after the eager validation pass in iter_stream_events has succeeded
    for membership-events content.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).
        routing: The effective routing policy.

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    _MembershipRow = tuple[tuple[int, int, str, str], tuple[object, ...], str]
    mem_rows: list[list[_MembershipRow]] = []

    # Build col_names per membership table (needed to construct after-images)
    col_names_by_source: dict[str, list[str]] = {}

    for ms in config.memberships:
        source_identity = f"{ms.owner_kind}__{ms.property}"
        sql = build_membership_events_sql(
            emit.sidecar, fork_path, ms.owner_kind, ms.property, ms.fields
        )
        rows = emit.query(sql, ())
        keyed = _rows_to_keyed(rows, source_identity)
        mem_rows.append(keyed)

        col_names_by_source[source_identity] = membership_fold_row_column_names(
            emit.sidecar, ms.owner_kind, ms.property, ms.fields
        )

    # k-way merge under the canonical key
    # (event_sim_time, event_class, source_identity, record_id)
    seq = 0
    for _merge_key, row, source_identity in heapq.merge(*mem_rows, key=lambda x: x[0]):
        seq += 1

        op = str(row[_IDX_OP])
        record_id = str(row[_IDX_RECORD_ID])
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])

        col_names = col_names_by_source[source_identity]
        after = _build_membership_after_image(row, col_names)
        ts = render_ts(event_sim_time, anchor)

        # Derive owner_kind and property from source_identity
        owner_kind, property_name = source_identity.split("__", 1)

        # Layer A: membership route attributes
        attrs = membership_route_attributes(owner_kind, property_name)

        # Layer B: apply routing policy to get topic
        topic = resolve_topic(routing, attrs)
        route_table = attrs["route_table"]

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=owner_kind,
            record_id=record_id,
            presentation_id=None,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=topic,
            route_table=route_table,
        )


def _iter_events_inner(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    sidecar: "Sidecar",
    routing: "RoutingConfig",
    subtype_indexes: dict[str, dict[str, str]],
) -> Iterator[StreamEvent]:
    """Materialize folds, merge, stamp seq/topic/route_table, and yield StreamEvents.

    Called only after the eager validation pass in iter_stream_events has succeeded.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).
        sidecar: The open emit's sidecar view.
        routing: The effective routing policy.
        subtype_indexes: Per-sub-typed-kind record_id -> sub_type indexes.

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    _KindRow = tuple[tuple[int, int, str, str], tuple[object, ...], str]
    kind_rows: list[list[_KindRow]] = []

    for kind_sel in config.kinds:
        kind = kind_sel.kind
        properties = frozenset(kind_sel.properties)

        sql = build_row_state_events_sql(emit.sidecar, fork_path, kind, properties)
        rows = emit.query(sql, ())

        # Apply types selection pre-merge for sub-typed kinds with an explicit list
        if kind_sel.types and kind in subtype_indexes:
            rows = _filter_rows_by_types(
                rows, kind, kind_sel.types, subtype_indexes[kind]
            )

        keyed = _rows_to_keyed(rows, kind)
        kind_rows.append(keyed)

    # Build col_names per kind (needed to construct after-images)
    col_names_by_kind: dict[str, list[str]] = {}
    for kind_sel in config.kinds:
        kind = kind_sel.kind
        properties = frozenset(kind_sel.properties)
        col_names_by_kind[kind] = record_fold_row_column_names(
            emit.sidecar, kind, properties
        )

    # k-way merge under the canonical key
    # (event_sim_time, event_class, source_identity=kind, record_id)
    seq = 0
    for _merge_key, row, kind in heapq.merge(*kind_rows, key=lambda x: x[0]):
        seq += 1

        op = str(row[_IDX_OP])
        record_id = str(row[_IDX_RECORD_ID])
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])

        col_names = col_names_by_kind[kind]
        has_pid = "presentation_id" in col_names

        # Extract presentation_id from row if present
        presentation_id: str | None = None
        if has_pid:
            pid_idx = col_names.index("presentation_id")
            raw_pid = row[pid_idx]
            presentation_id = str(raw_pid) if raw_pid is not None else None

        after = _build_after_image(row, col_names, op)
        ts = render_ts(event_sim_time, anchor)

        # Layer A: derive route attributes
        sub_type: str | None = None
        kind_is_subtyped = _is_kind_subtyped(kind, sidecar)
        if kind_is_subtyped and kind in subtype_indexes:
            sub_type = subtype_indexes[kind].get(record_id)
        attrs = route_attributes(kind_is_subtyped, kind, sub_type)

        # Layer B: apply routing policy to get topic
        topic = resolve_topic(routing, attrs)
        route_table = attrs["route_table"]

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=kind,
            record_id=record_id,
            presentation_id=presentation_id,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=topic,
            route_table=route_table,
        )


def iter_stream_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
) -> Iterator[StreamEvent]:
    """Yield the stream's events in canonical total order with seq stamped.

    Split into two phases: an eager validation pass that runs at call time
    (before the first next()), then an inner generator that materializes the
    per-content folds, runs the k-way merge, stamps seq, renders ts, stamps
    topic/route_table, and yields StreamEvents. Dispatches on config.content:
    'state-changes' runs the kind-validation pass and the state-changes inner
    iterator; 'membership-events' runs the membership-validation pass and the
    membership inner iterator. Each failure raises ExportError with a friendly,
    content-specific message.

    See § Ordering and `seq`, § Timestamp rendering, § Business Rules.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None.

    Returns:
        An iterator of StreamEvent in global seq order.

    Raises:
        ExportError: The validation pass failed — more than one branch (single-branch
            guard), an unresolvable kind or membership table, an unresolvable
            property or field, a state-changes property selecting a non-exempt
            slice_only column, or a routing business rule. Raised at call time,
            before the first next().
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    routing = config.routing if config.routing is not None else _default_routing()

    if config.content == "membership-events":
        # Eager membership pass — MembershipResolvable, MembershipFieldResolvable
        fork_path = _validate_memberships(emit, config, routing)
        return _iter_membership_events_inner(emit, config, anchor, fork_path, routing)

    # state-changes: eager validation pass — before any generator suspension
    fork_path = _validate_kinds(emit, config, routing)

    subtype_indexes = _build_subtype_indexes(emit, config, emit.sidecar)

    return _iter_events_inner(
        emit, config, anchor, fork_path, emit.sidecar, routing, subtype_indexes
    )


def build_topic_set(
    config: "StreamConfig",
    sidecar: "Sidecar",
) -> tuple[str, ...]:
    """Enumerate the run's full topic set, including declared-but-empty topics.

    Derives all selected route-attribute mappings from the config — fanning each
    selected kind out over its declared sub-types from
    ``Sidecar.subtype_values`` (the ``<kind>_type`` discriminator domain) — then
    delegates to ``enumerate_topics``. A bare-string-role kind that carries a
    discriminator domain (e.g. ``entity``) fans out exactly as ``actor`` does; a
    kind with no domain is a single topic.

    Args:
        config: The validated streaming configuration.
        sidecar: The open emit's sidecar view — the source of each selected
            kind's declared sub-type set via ``subtype_values``.

    Returns:
        The ordered, de-duplicated topic set the sink must materialize. Sub-type
        fan-out follows ``enum_domains`` declaration order (see the design doc
        § Semantics for the full ordering rule).
    """
    routing = config.routing if config.routing is not None else _default_routing()
    all_attrs = _build_all_selected_attributes(config, sidecar)
    return enumerate_topics(routing, all_attrs)
