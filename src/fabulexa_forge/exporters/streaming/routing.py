"""Layer-A routing surface for the streaming exporter.

Derives per-event route attributes (kind, route_table, sub_type) — the
per-event leaf logical table, consumed solely by the Debezium
`source_table` masquerade and the discriminator index. Layer B (topic
template rendering, groups regrouping) is retired: topic naming is now the
author-declared stream name (`StreamDeclaration.name`), carried straight
through by the engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.errors import TableNotFoundError

if TYPE_CHECKING:
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
        kind is sub-typed.

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


def membership_route_attributes(
    owner_kind: str,
    property_name: str,
) -> dict[str, str]:
    """Build one membership event's Layer-A route attributes.

    The membership analog of route_attributes: {owner_kind, property, route_table}
    with route_table = f"{owner_kind}__{property_name}". No sub_type. The sole place
    that interprets the membership relation identity. Total over its inputs — raises
    nothing.

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
