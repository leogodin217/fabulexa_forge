"""Row selection: `where` (both stream shapes) and membership owner
`sub_types`, over the promoted mode-neutral selection spine
(`exporters.selection_spine`). Streaming never imports `exporters.source` —
its own `_resolve_stream_where` gate walk mirrors source's
`_resolve_where_selection` semantics deliberately, not as a parametrized
reuse: source's resolver entangles `key_form` / label / error selection in a
way that would make sharing a worse abstraction than repeating this ~50-line
walk (design doc § Row selection).

A kind stream's `sub_types` stay the shipped discriminator-index device the
engine already carries; this module only resolves `where`. A membership
stream's owner `sub_types` and `where` resolve together here, through the
spine's owner-kind parent lookup.

Layer-direction invariant: imports the reader, config.models
(`MembershipStream` at runtime for the shape `isinstance` dispatch;
`KindStream` / `PredicateValue` TYPE_CHECKING only), `fabulexa_forge._sql`
(`cast_predicate_element`), `derivations.guard` (`require_single_branch`),
the mode-neutral `exporters.populations` (`Population`) and
`exporters.selection_spine` (`WhereEntry`, `build_selection_spine_sql`,
`check_where_values_observed`, `where_predicate_elements`), `errors`, and
`exporters.notices` (TYPE_CHECKING only). Never imports `exporters.source.*`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.config.models import KindStream, PredicateValue
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import cast_predicate_element
from fabulexa_forge.config.models import MembershipStream
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import (
    StreamWhereColumnUnresolved,
    StreamWhereNotConstant,
    StreamWhereOnDiscriminator,
    StreamWhereValueUncastable,
)
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.selection_spine import (
    WhereEntry,
    build_selection_spine_sql,
    check_where_values_observed,
    where_predicate_elements,
)

#: Prefix marking a records-table scalar-property column (mirrors
#: `exporters.selection_spine._PROP_PREFIX`; streaming keeps its own copy
#: rather than importing a private module constant).
_PROP_PREFIX = "prop__"


def _resolve_stream_where(
    sidecar: "Sidecar",
    where: "Mapping[str, PredicateValue]",
    subject_kind: str,
    stream_name: str,
) -> tuple[WhereEntry, ...]:
    """The streaming-local constant-column gate walk over the shared
    row-selection primitives (mirrors source's `_resolve_where_selection`
    semantics, this design's `stream '{name}'` messages and `StreamWhere*`
    classes).

    Args:
        sidecar: The open emit's sidecar.
        where: The stream's declared `where` mapping (present and non-empty;
            callers skip the call when the field is absent).
        subject_kind: The stream's subject kind — the declared kind for a
            kind stream, the owner kind for a membership stream.
        stream_name: The declaring stream's name, for messages.

    Returns:
        The resolved entries, `where` declaration order.

    Raises:
        StreamWhereColumnUnresolved: A key resolves to no payload property
            of the subject kind.
        StreamWhereOnDiscriminator: A key names the subject kind's declared
            discriminator.
        StreamWhereNotConstant: A resolved column's `temporal_class` is not
            `constant`.
        StreamWhereValueUncastable: An element fails its column's
            sidecar-declared cast.
    """
    label = f"stream '{stream_name}'"
    table_name = f"records__{subject_kind}"
    cols = sidecar.columns(table_name)
    bare_names = {
        c.name[len(_PROP_PREFIX) :] for c in cols if c.name.startswith(_PROP_PREFIX)
    }
    col_types = {c.name: c.type for c in cols}
    discriminator_col = (
        f"{_PROP_PREFIX}{subject_kind}_type"
        if sidecar.subtype_values(subject_kind)
        else None
    )

    entries: list[WhereEntry] = []
    for key, value in where.items():
        if key not in bare_names:
            raise StreamWhereColumnUnresolved(
                f"{label}: where key '{key}' is not a payload property"
                f" of kind '{subject_kind}'"
            )
        source_column = f"{_PROP_PREFIX}{key}"
        if source_column == discriminator_col:
            raise StreamWhereOnDiscriminator(
                f"{label}: where key '{key}' is the discriminator; use sub_types"
            )

        temporal_class = sidecar.temporal_class(table_name, source_column)
        if temporal_class != "constant":
            raise StreamWhereNotConstant(
                f"{label}: where key '{key}' is not a constant-class property"
                f" of kind '{subject_kind}'"
            )

        sql_type = col_types[source_column]
        elements = where_predicate_elements(value)
        typed_values: list[object] = []
        for element in elements:
            try:
                typed_values.append(cast_predicate_element(element, sql_type))
            except ValueError as exc:
                raise StreamWhereValueUncastable(
                    f"{label}: where value '{element}' does not cast to"
                    f" {sql_type} for '{key}'"
                ) from exc

        entries.append(
            WhereEntry(
                key=key,
                source_column=source_column,
                sql_type=sql_type,
                value=value,
                typed_values=tuple(typed_values),
            )
        )
    return tuple(entries)


def _where_unobserved_message(
    stream_name: str, key: str, element: str, wholly_unobserved: bool
) -> str:
    """Render one stream `where`-value-unobserved notice's message.

    The shipped two-case wording (dimensional's
    `check_discriminator_value_observed` granularity) with this design's
    stream nouns: a wholly-unobserved entry states the declared-but-empty
    topic; a partially-covered entry states only that this element
    contributes no events.

    Args:
        stream_name: The declaring stream's name.
        key: The `where` key as written.
        element: The unobserved element.
        wholly_unobserved: Whether every element of the entry's value is
            unobserved.

    Returns:
        The notice message text.
    """
    label = f"stream '{stream_name}'"
    if wholly_unobserved:
        return (
            f"{label}: where value '{element}' for '{key}' not observed;"
            " the topic will be empty"
        )
    return (
        f"{label}: where value '{element}' for '{key}' not observed;"
        " it contributes no events"
    )


def _stream_populations(
    sidecar: "Sidecar", kind: str, sub_types: "tuple[str, ...] | None"
) -> tuple[Population, ...]:
    """The addressed population set for one subject kind, gate-free.

    Sub-type existence and domain membership are already gated before this
    is called (`_validate_kind_stream` / `_validate_membership_stream`), so
    this is a plain resolution, not a re-validation: `sub_types` (when
    given) or the kind's full declared domain, one atom per value; a flat
    kind resolves to the single `(kind, None)` atom.

    Args:
        sidecar: The open emit's sidecar.
        kind: The subject kind.
        sub_types: The addressed sub-type subset, or None for the full
            domain.

    Returns:
        The resolved population atoms.
    """
    domain = sidecar.subtype_values(kind)
    if not domain:
        return (Population(kind=kind, sub_type=None),)
    values = sub_types if sub_types is not None else domain
    return tuple(Population(kind=kind, sub_type=value) for value in values)


def resolve_stream_selection(
    emit: "Emit",
    stream: "KindStream | MembershipStream",
    notice_sink: "NoticeSink",
) -> frozenset[str] | None:
    """Compute a stream's satisfying record set (owner set, for a
    membership stream) from its declared selection, or None when the
    stream declares no selection this function owns: a kind stream's
    `sub_types` stay the shipped discriminator-index device (None when it
    declares no `where`); a membership stream's owner `sub_types` and
    `where` resolve together here through the parent-lookup spine (None
    only when it declares neither).

    Compiles the predicate through the shared rendering authority against
    the subject kind's records spine (via the shared selection-spine
    parent lookup for a membership stream); the constant-column gate and
    the plan-time value casts run first. Emits the per-element
    `discriminator-value-unobserved` notice for each `where` element
    outside its column's declared `enum_domains` entry.

    Args:
        emit: The open emit.
        stream: The declared stream.
        notice_sink: The caller-supplied sink the out-of-domain notices
            flow through.

    Returns:
        The record_ids whose events the stream carries — codec-encoded
        strings, the type the engine's shipped str-keyed row-scoping
        device compares — or None when the stream declares no selection
        this function owns (all rows in scope).

    Raises:
        StreamWhereNotConstant: A `where` key names a tracked or
            slice_only property.
        StreamWhereOnDiscriminator: A `where` key names the subject kind's
            discriminator.
        StreamWhereColumnUnresolved: A `where` key resolves to no payload
            property of the subject kind.
        StreamWhereValueUncastable: A value fails its column's cast.
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)

    if isinstance(stream, MembershipStream):
        subject_kind = stream.membership.kind
        sub_types = tuple(stream.sub_types) if stream.sub_types is not None else None
        if sub_types is None and not stream.where:
            return None
        populations = _stream_populations(sidecar, subject_kind, sub_types)
    else:
        subject_kind = stream.kind
        if not stream.where:
            return None
        populations = _stream_populations(sidecar, subject_kind, None)

    entries = _resolve_stream_where(
        sidecar, stream.where or {}, subject_kind, stream.name
    )
    check_where_values_observed(
        sidecar,
        entries,
        subject_kind,
        notice_sink,
        lambda key, element, wholly: _where_unobserved_message(
            stream.name, key, element, wholly
        ),
    )

    spine_sql = build_selection_spine_sql(
        sidecar, fork_path, subject_kind, populations, entries
    )
    if spine_sql is None:
        return None
    rows = emit.query(spine_sql, ())
    return frozenset(str(row[0]) for row in rows)
