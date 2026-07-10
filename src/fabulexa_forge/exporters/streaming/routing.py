"""Layer-A and Layer-B routing surface for the streaming exporter.

Layer A: derive the per-event route attributes (kind, route_table, sub_type).
Layer B: apply the RoutingConfig policy to produce a resolved topic name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.errors import TableNotFoundError

if TYPE_CHECKING:
    from fabulexa_forge.config.models import RoutingConfig
    from fabulexa_forge.reader.emit import Emit


def route_attributes(
    is_subtyped: bool,
    kind: str,
    sub_type: str | None,
) -> dict[str, str]:
    """Build one event's Layer-A route attributes for state-changes content.

    The sole owner of the ``route_table`` (leaf) rule: the sub-type value for a
    sub-typed kind, the bare kind otherwise. Decoupled from the reader — the
    caller decides sub-typed-ness (from ``Sidecar.subtype_values``) and passes
    the verdict as a plain flag, so Layer A carries no ``record_roles`` or
    discriminator-source dependency.

    Args:
        is_subtyped: Whether ``kind`` splits per sub-type — True iff
            ``Sidecar.subtype_values(kind)`` is non-empty.
        kind: The base-layer record kind.
        sub_type: The record's ``prop__<kind>_type`` discriminator value when the
            kind is sub-typed; None when it is not.

    Returns:
        A mapping with keys 'kind' and 'route_table', plus 'sub_type' when the
        kind is sub-typed. Used as template variables for ``resolve_topic`` and
        ``enumerate_topics``.

    Raises:
        ValueError: ``is_subtyped`` is True but ``sub_type`` is None, or
            ``is_subtyped`` is False but ``sub_type`` is non-None — an
            inconsistent request.
    """
    if is_subtyped and sub_type is None:
        raise ValueError(f"kind '{kind}' is sub-typed but sub_type is None")
    if not is_subtyped and sub_type is not None:
        raise ValueError(
            f"kind '{kind}' is not sub-typed but sub_type was given: {sub_type!r}"
        )

    if is_subtyped:
        assert sub_type is not None  # narrowed above
        return {"kind": kind, "route_table": sub_type, "sub_type": sub_type}

    return {"kind": kind, "route_table": kind}


def _member_to_target(routing: "RoutingConfig") -> dict[str, str]:
    """Build a reverse index: member base-topic -> group target topic.

    Args:
        routing: The resolved Layer-B policy.

    Returns:
        A mapping member -> target for every member in every group.
    """
    index: dict[str, str] = {}
    for target, members in routing.groups.items():
        for member in members:
            index[member] = target
    return index


def resolve_topic(
    routing: "RoutingConfig",
    attributes: dict[str, str],
) -> str:
    """Apply Layer-B policy to one event's route attributes to produce its topic.

    Renders routing.topic_template against `attributes`, then remaps the rendered name
    to a groups target when it is a member of one. Content-agnostic: it reads only
    `attributes` and the policy, never kind / sub_type by name.

    Args:
        routing: The resolved Layer-B policy.
        attributes: The event's route attributes from route_attributes.

    Returns:
        The resolved topic name.

    Raises:
        KeyError: The template references a placeholder absent from `attributes` (e.g.
            {sub_type} for a non-sub-typed kind) — surfaced to the author as a config
            error by the validation pass before any event is routed.
    """
    base_name = routing.topic_template.format(**attributes)
    member_to_target = _member_to_target(routing)
    return member_to_target.get(base_name, base_name)


def enumerate_topics(
    routing: "RoutingConfig",
    selected_attributes: list[dict[str, str]],
) -> tuple[str, ...]:
    """Enumerate the run's full topic set, including declared-but-empty topics.

    The union of each selected route attribute's resolved topic and every groups target
    topic, in deterministic order (selection order, then group-config order,
    de-duplicated).

    Args:
        routing: The resolved Layer-B policy.
        selected_attributes: One route-attribute mapping per selected (kind, sub_type)
            in enumeration order — every sub-type the run may emit, whether or not
            it has rows.

    Returns:
        The ordered, de-duplicated topic set the sink must materialize. De-duplication
        keeps each topic's first occurrence: a groups target that coincides with an
        already-rendered name stays at that earlier position rather than reappearing in
        group-config order.
    """
    seen: set[str] = set()
    topics: list[str] = []

    for attrs in selected_attributes:
        topic = resolve_topic(routing, attrs)
        if topic not in seen:
            seen.add(topic)
            topics.append(topic)

    for target in routing.groups:
        if target not in seen:
            seen.add(target)
            topics.append(target)

    return tuple(topics)


def membership_route_attributes(
    owner_kind: str,
    property_name: str,
) -> dict[str, str]:
    """Build one membership event's Layer-A route attributes.

    The membership analog of route_attributes: {owner_kind, property, route_table}
    with route_table = f"{owner_kind}__{property_name}". No sub_type. The sole place
    that interprets the membership relation identity; used as template variables for
    resolve_topic and enumerate_topics. Total over its inputs — raises nothing.

    Returns:
        A mapping with keys 'owner_kind', 'property', and 'route_table'.
    """
    return {
        "owner_kind": owner_kind,
        "property": property_name,
        "route_table": f"{owner_kind}__{property_name}",
    }


def resolve_subtype_index(
    emit: "Emit",
    kind: str,
) -> dict[str, str]:
    """Index a sub-typed kind's records by their immutable discriminator value.

    Reads records__<kind>.prop__<kind>_type from the spine, independent of the selected
    properties. Called only for kinds that Sidecar.subtype_values reports as sub-typed.

    Args:
        emit: The open emit (reader + connection).
        kind: A sub-typed record kind.

    Returns:
        A mapping record_id -> sub_type value. Total over the kind's records; the
        discriminator is contract-guaranteed present and in-domain.

    Raises:
        ExportError: records__<kind> or its prop__<kind>_type discriminator column
            does not resolve in the sidecar (a defensive backstop; the contract
            guarantees both for a sub-typed kind).
    """
    table_name = f"records__{kind}"
    discriminator_col = f"prop__{kind}_type"

    # Verify table and discriminator column exist in sidecar
    try:
        cols = emit.sidecar.columns(table_name)
    except TableNotFoundError:
        raise ExportError(
            f"resolve_subtype_index: table '{table_name}' not found in sidecar"
        ) from None

    col_names = {col.name for col in cols}
    if discriminator_col not in col_names:
        raise ExportError(
            f"resolve_subtype_index: discriminator column '{discriminator_col}'"
            f" not found in '{table_name}'"
        )

    rows = emit.query(
        f'SELECT record_id, "{discriminator_col}" FROM "{table_name}"',
        (),
    )
    return {str(record_id): str(sub_type) for record_id, sub_type in rows}
