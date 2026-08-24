"""Streaming engine: validation pass, per-stream fold materialization, k-way merge.

Produces StreamEvent objects in the canonical total order with seq stamped and
ts rendered from the EffectiveAnchor. Materializes one fold per declared
stream (not per kind/table): a kind-shaped stream's event set is
payload-independent (change scope = its `only` / audited-minus-`ignore` /
audited property set — the audited default, unset, is byte-identical to the
shipped full-property-set invocation; projection = the stream's declared
properties), and every stream's rows merge under a
canonical key whose source-identity component is the declared stream name.
Also resolves the message-key election (`exporters.election`) and renders the
elected key and after-image identity through it (design doc § Message-key
election). A declared stream's numeric `render` map (`decimal` /
`json_precision`) applies at the codec seam — the post-fold SELECT
`_wrap_stream_render_sql` composes ahead of the after-image assembly,
through the same `fabulexa_forge._sql` rendering authorities the table modes
compose (design doc § Streaming attach). Naming and vocabulary — after-image
output keys, member-kind values, the envelope `kind` — resolve through
`presentation.py`, the single naming authority also read by the driver's
Debezium value-schema builders (design doc § Output-name resolution, § Kind
vocabulary). Row selection (`where`, membership owner `sub_types`) resolves
through `selection.py`'s `resolve_stream_selection`, once per stream in the
eager pass; the engine's own drop device (`_filter_rows_by_selection`)
narrows each stream's fold rows post-fold, composing independently with the
shipped `sub_types` discriminator-index drop on a kind stream (design doc §
Row selection). Layer-direction invariant: imports derivations, config,
reader, anchor, `fabulexa_forge._sql`, the mode-neutral election module, and
the sibling presentation / selection modules, and errors — never writers or
CLI.
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Iterator, Literal, Sequence, cast

from fabulexa_forge._sql import render_decimal_expr, render_json_precision_expr
from fabulexa_forge.anchor import render_ts
from fabulexa_forge.config.models import (
    DecimalElection,
    JsonPrecisionElection,
    KindStream,
    MembershipStream,
)
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
from fabulexa_forge.errors import (
    DecimalSourceIsDouble,
    ExportError,
    JsonPrecisionSourceIsVarchar,
    RenderKeyResolves,
    StreamChangeScopeUnresolvable,
    StreamOutputNameCollision,
    StreamRenameUnresolvable,
)
from fabulexa_forge.exporters.election import (
    _presentation_key_sql,
    _record_index_sql,
    check_edge_union_safety,
    check_elected_key_unique,
    check_identity_election,
    resolve_election,
)
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.exporters.streaming.presentation import (
    apply_kind_vocabulary,
    resolve_membership_output_columns,
    resolve_stream_envelope_kind,
    resolve_stream_kind_vocabulary,
    resolve_stream_output_columns,
)
from fabulexa_forge.exporters.streaming.routing import (
    kind_reference_targets,
    known_records_kinds,
    membership_reference_fields,
    membership_route_attributes,
    resolve_subtype_index,
    route_attributes,
)
from fabulexa_forge.exporters.streaming.selection import resolve_stream_selection
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.reader.errors import TableNotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface, StreamConfig
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.notices import NoticeSink
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
    noun: str = "property",
) -> None:
    """Enforce StreamPropertySliceOnly over one kind-shaped stream's entries.

    No entry may resolve to a non-exempt slice_only prop__<p> column of
    records__<kind>. Refuse-only; emits nothing. Shared by the `properties`
    projection check and the `only` / `ignore` change-scope check
    (`noun` names which surface, for the message).

    Args:
        sidecar: The open emit's sidecar.
        name: The declaring stream's name.
        kind: The record kind owning the selected properties.
        properties: The selected property names (bare, prop__ stripped).
        noun: The message's noun for one entry — 'property', or 'only
            entry' / 'ignore entry' for the change-scope surface.

    Raises:
        ExportError: A selected property resolves to a non-exempt slice_only
            column. Message leads with the stream name.
        TemporalClassUnavailableError: Propagated.
    """
    for prop in properties:
        column_name = f"prop__{prop}"
        if is_non_exempt_slice_only(sidecar, kind, column_name):
            raise ExportError(
                f"stream '{name}': stream kind '{kind}': {noun} '{prop}'"
                " is temporal_class: slice_only; it cannot ride the"
                " state-changes after-image"
            )


def _check_stream_change_scope(
    sidecar: "Sidecar",
    name: str,
    kind: str,
    sidecar_prop_names: "set[str]",
    only: "Sequence[str] | None",
    ignore: "Sequence[str] | None",
) -> None:
    """Enforce StreamChangeScopeUnresolvable and the slice_only refusal over
    one kind-shaped stream's `only` / `ignore` change-scope narrowing.

    Args:
        sidecar: The open emit's sidecar.
        name: The declaring stream's name.
        kind: The record kind owning the audited property set.
        sidecar_prop_names: Every bare prop__ name declared on
            records__<kind> (the stream's already-resolved column set).
        only: The stream's declared `only` entries, or None.
        ignore: The stream's declared `ignore` entries, or None.

    Raises:
        StreamChangeScopeUnresolvable: An entry names no prop__ column of
            kind.
        ExportError: An entry names a non-exempt slice_only column
            (StreamPropertySliceOnly's extended shape).
        TemporalClassUnavailableError: Propagated.
    """
    entries, field = (only, "only") if only is not None else (ignore, "ignore")
    if entries is None:
        return
    for prop in entries:
        if prop not in sidecar_prop_names:
            raise StreamChangeScopeUnresolvable(
                f"stream '{name}': {field} entry '{prop}' has no"
                f" prop__{prop} column on kind '{kind}'"
            )
    _check_stream_properties_slice_only(
        sidecar, name, kind, entries, noun=f"{field} entry"
    )


#: Per numeric-only streaming election kind: the required declared source
#: type and the error class its gate raises — streaming's own addressing of
#: the decimal / json_precision source-type gates (no instant election
#: reaches streaming; design doc § Streaming attach).
_STREAM_RENDER_SOURCE_GATES: dict[str, tuple[str, type[ExportError], str]] = {
    "decimal": (
        "DOUBLE",
        DecimalSourceIsDouble,
        "decimal rendering requires a DOUBLE source",
    ),
    "json_precision": (
        "VARCHAR",
        JsonPrecisionSourceIsVarchar,
        "json_precision requires a VARCHAR JSON payload source",
    ),
}


def _verify_stream_render_source_type(
    stream_name: str,
    key: str,
    column_name: str,
    election: "DecimalElection | JsonPrecisionElection",
    col_types: dict[str, str],
) -> None:
    """Enforce one stream render entry's source-type gate.

    Args:
        stream_name: The declaring stream's name, for the error.
        key: The render-map key (bare property or element-schema field name).
        column_name: The resolved after-image column (`prop__<key>` /
            `elem__<key>`) whose declared type is checked.
        election: The elected form.
        col_types: The addressed table's column name -> declared DuckDB type.

    Raises:
        DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar: `column_name`'s
            declared type fails the election's source-type gate.
    """
    form = "decimal" if isinstance(election, DecimalElection) else "json_precision"
    expected_type, error_cls, reason = _STREAM_RENDER_SOURCE_GATES[form]
    sql_type = col_types.get(column_name)
    if sql_type is None or sql_type.upper() != expected_type:
        got = sql_type if sql_type is not None else "no declared type"
        raise error_cls(
            f"render key '{key}' on stream '{stream_name}': {reason} (got {got})"
        )


def _verify_stream_render_key_in_projection(
    stream_name: str, key: str, projected: "frozenset[str]", noun: str
) -> None:
    """Enforce RenderKeyResolves' streaming domain: a render key names a
    member of the stream's own projection.

    Args:
        stream_name: The declaring stream's name.
        key: The render-map key (bare property or element-schema field name).
        projected: The stream's declared projection (`properties` /
            `fields`), as a set.
        noun: 'property' or 'field', for the message.

    Raises:
        RenderKeyResolves: `key` is not a member of `projected`.
    """
    if key not in projected:
        raise RenderKeyResolves(
            f"stream '{stream_name}': render key '{key}' does not name a"
            f" declared {noun} of the stream's projection"
        )


def _validate_kind_stream_render(
    stream: "KindStream", col_types: dict[str, str]
) -> None:
    """Run the streaming render-map business rules for one kind-shaped stream.

    Args:
        stream: The kind-shaped stream declaration.
        col_types: records__<kind>'s column name -> declared DuckDB type.

    Raises:
        RenderKeyResolves: A render key is not a member of `stream.properties`.
        DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar: The election's
            source-type gate fails.
    """
    if not stream.render:
        return
    properties = frozenset(stream.properties)
    for key, election in stream.render.items():
        _verify_stream_render_key_in_projection(
            stream.name, key, properties, "property"
        )
        _verify_stream_render_source_type(
            stream.name, key, f"prop__{key}", election, col_types
        )


def _validate_membership_stream_render(
    stream: "MembershipStream", col_types: dict[str, str], col_names: "set[str]"
) -> None:
    """Run the streaming render-map business rules for one membership stream.

    Args:
        stream: The membership-shaped stream declaration.
        col_types: The membership table's column name -> declared DuckDB type.
        col_names: The membership table's column names.

    Raises:
        RenderKeyResolves: A render key is not a member of `stream.fields`,
            or names a reference field (outside the typed-election domain —
            reference identity is key election's surface).
        DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar: The election's
            source-type gate fails.
    """
    if not stream.render:
        return
    fields = frozenset(stream.fields)
    for key, election in stream.render.items():
        _verify_stream_render_key_in_projection(stream.name, key, fields, "field")
        if f"member__{key}__kind" in col_names:
            raise RenderKeyResolves(
                f"stream '{stream.name}': render key '{key}' names a"
                " reference field; typed elections require a scalar"
                " elem__ column"
            )
        _verify_stream_render_source_type(
            stream.name, key, f"elem__{key}", election, col_types
        )


def _validate_kind_stream(sidecar: "Sidecar", stream: "KindStream") -> None:
    """Run StreamKindResolvable through the change-scope gates for one stream.

    Args:
        sidecar: The open emit's sidecar view.
        stream: The kind-shaped stream declaration.

    Raises:
        ExportError: StreamKindResolvable, StreamSubTypesRequireSubtyping,
            StreamSubTypesDeclared, StreamPropertyResolvable, or
            StreamPropertySliceOnly (over `properties` or an `only` /
            `ignore` entry) fails. Message leads with the stream name.
        StreamChangeScopeUnresolvable: An `only` / `ignore` entry names no
            prop__ column of the stream's kind.
        RenderKeyResolves: A `render` key is not a member of
            `stream.properties`.
        DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar: A `render`
            entry's source-type gate fails.
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
    _check_stream_change_scope(
        sidecar, stream.name, kind, sidecar_prop_names, stream.only, stream.ignore
    )
    col_types = {c.name: c.type for c in cols}
    _validate_kind_stream_render(stream, col_types)


def _validate_membership_stream(sidecar: "Sidecar", stream: "MembershipStream") -> None:
    """Run MembershipResolvable and MembershipFieldResolvable for one stream.

    Args:
        sidecar: The open emit's sidecar view.
        stream: The membership-shaped stream declaration.

    Raises:
        ExportError: MembershipResolvable or MembershipFieldResolvable
            fails, or owner `sub_types` addresses a non-sub-typed owner
            kind or an undeclared owner sub-type. Message leads with the
            stream name.
        RenderKeyResolves: A `render` key is not a member of `stream.fields`,
            or names a reference field.
        DecimalSourceIsDouble, JsonPrecisionSourceIsVarchar: A `render`
            entry's source-type gate fails.
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

    if stream.sub_types is not None:
        owner_domain = sidecar.subtype_values(kind)
        if not owner_domain:
            raise ExportError(
                f"stream '{stream.name}': owner kind '{kind}' is not"
                " sub-typed; sub_types is not addressable"
            )
        declared_set = set(owner_domain)
        for value in stream.sub_types:
            if value not in declared_set:
                raise ExportError(
                    f"stream '{stream.name}': sub_type '{value}' is not"
                    f" declared for owner kind '{kind}'"
                )

    col_names = {c.name for c in cols}
    if stream.fields:
        for field in stream.fields:
            if not field_resolves(col_names, field):
                raise ExportError(
                    f"stream '{stream.name}': field '{field}'"
                    " has no elem__/member__ column"
                )

    col_types = {c.name: c.type for c in cols}
    _validate_membership_stream_render(stream, col_types, col_names)


def _resolve_kind_stream_surface(
    sidecar: "Sidecar",
    election: "Election",
    stream: "KindStream",
) -> "KeySurface":
    """Gate and resolve one kind-shaped stream's uniform elected surface.

    The spanned populations are the stream's declared `sub_types`, or the
    kind's full declared domain under the shorthand (design doc § Message-key
    election). A flat kind needs no gate — one population, trivially uniform.

    Args:
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        stream: The kind-shaped stream declaration.

    Returns:
        The stream's uniform elected surface ('record_id' under no election).

    Raises:
        ElectionMixedIdentity: The spanned populations elect differing surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election whose spanned
            key spaces contain a pairwise-unsafe pair.
    """
    domain = sidecar.subtype_values(stream.kind)
    if not domain:
        return election.surface_for(stream.kind, None)
    populations = tuple(stream.sub_types) if stream.sub_types is not None else domain
    check_identity_election(
        election, stream.kind, populations, f"stream '{stream.name}'"
    )
    return election.surface_for(stream.kind, populations[0])


def _resolve_membership_stream_surface(
    sidecar: "Sidecar",
    election: "Election",
    stream: "MembershipStream",
) -> "KeySurface":
    """Gate and resolve one membership-shaped stream's owner elected surface.

    The uniformity gate ranges over the stream's addressed owner population
    set — the declared owner `sub_types`, or the owner kind's full declared
    domain when absent (design doc § Message-key election, § Row selection's
    uniformity-granularity row): a mixed-election owner kind is splittable
    per sub-type across streams, not unconditionally refused whole-domain.

    Args:
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        stream: The membership-shaped stream declaration.

    Returns:
        The addressed owner population set's uniform elected surface
        ('record_id' under no election).

    Raises:
        ElectionMixedIdentity: The addressed owner population set elects
            differing surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election whose owner
            key spaces contain a pairwise-unsafe pair.
    """
    owner_kind = stream.membership.kind
    domain = sidecar.subtype_values(owner_kind)
    if not domain:
        return election.surface_for(owner_kind, None)
    populations = tuple(stream.sub_types) if stream.sub_types is not None else domain
    check_identity_election(
        election, owner_kind, populations, f"stream '{stream.name}'"
    )
    return election.surface_for(owner_kind, populations[0])


def _gate_kind_stream_reference_edges(
    sidecar: "Sidecar",
    election: "Election",
    stream: "KindStream",
) -> None:
    """Run the edge union-safety gate over one kind-shaped stream's reference props.

    Streaming's admitted set for a reference column is the target kind's full
    declared domain (design doc § Message-key election). `kind_reference_
    targets` already restricts to targets present in the emit, so every
    target here resolves through `election`.

    Args:
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        stream: The kind-shaped stream declaration.

    Raises:
        ElectionUnionUnsafe: An admitted target domain's resolved key spaces
            contain a pairwise-unsafe pair.
    """
    known_kinds = frozenset(known_records_kinds(sidecar))
    targets = kind_reference_targets(
        sidecar, stream.kind, stream.properties, known_kinds
    )
    for prop, target_kind in targets.items():
        domain = sidecar.subtype_values(target_kind)
        check_edge_union_safety(
            election, target_kind, domain, f"stream '{stream.name}'.prop__{prop}"
        )


def _gate_membership_stream_edges(
    sidecar: "Sidecar",
    election: "Election",
    stream: "MembershipStream",
) -> None:
    """Run the edge union-safety gate over one membership stream's reference fields.

    Streaming's admitted set for a member field is every known records kind
    (design doc § Message-key election — "per member kind"), each gated
    independently over its own domain.

    Args:
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        stream: The membership-shaped stream declaration.

    Raises:
        ElectionUnionUnsafe: Some admitted kind's own domain's resolved key
            spaces contain a pairwise-unsafe pair.
    """
    reference_fields = membership_reference_fields(
        sidecar, stream.membership.kind, stream.membership.property, stream.fields
    )
    if not reference_fields:
        return
    for field in reference_fields:
        for kind in known_records_kinds(sidecar):
            domain = sidecar.subtype_values(kind)
            check_edge_union_safety(
                election,
                kind,
                domain,
                f"stream '{stream.name}'.member__{field} (member kind '{kind}')",
            )


def resolve_stream_surfaces(
    sidecar: "Sidecar",
    election: "Election",
    config: "StreamConfig",
) -> dict[str, "KeySurface"]:
    """Gate and resolve every declared stream's uniform elected surface.

    Runs the identity-uniformity gate (ElectionMixedIdentity), the
    presentation_id union-safety gate (ElectionUnionUnsafe) over each
    stream's own spanned populations, and the edge union-safety gate over
    every stream's reference / member-field columns. A pure function of
    (sidecar, election, config) — called by the engine's own eager
    validation pass and again by the driver's Debezium value-schema builder;
    both computations are the same pure function, so they cannot disagree
    (mirrors `exporters.base.engine`'s recompute-not-thread posture).

    Args:
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        config: The validated streaming configuration.

    Returns:
        Declaring stream name -> the stream's uniform elected surface.

    Raises:
        ElectionMixedIdentity: A stream's spanned populations elect differing
            surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election's spanned key
            spaces, or an edge's admitted target key spaces, contain a
            pairwise-unsafe pair.
    """
    surfaces: dict[str, "KeySurface"] = {}
    for stream in config.streams:
        if isinstance(stream, KindStream):
            surfaces[stream.name] = _resolve_kind_stream_surface(
                sidecar, election, stream
            )
            _gate_kind_stream_reference_edges(sidecar, election, stream)
        else:
            surfaces[stream.name] = _resolve_membership_stream_surface(
                sidecar, election, stream
            )
            _gate_membership_stream_edges(sidecar, election, stream)
    return surfaces


def build_elected_identity_index(
    emit: "Emit",
    fork_path: str,
    kind: str,
    surface: "Literal['record_index', 'presentation_id']",
) -> dict[str, str]:
    """record_id -> codec-rendered elected value for one kind, end-of-tape.

    Composes the record-index or presentation-key derivation at the
    end-of-tape entry point and runs the elected-key uniqueness guard over
    the drawn rows: rows == DISTINCT record_id == DISTINCT elected value,
    elected value non-NULL. The one data-touching election check
    (key-election.md § The elected-key uniqueness guard).

    Args:
        emit: The open emit.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The records kind.
        surface: 'record_index' or 'presentation_id' (a 'record_id' election
            composes no relation and never calls this).

    Returns:
        The identity map, every value a non-null str.

    Raises:
        ElectedKeyDuplicate: The guard failed; names the kind and the surface.
    """
    sidecar = emit.sidecar
    relation_sql = (
        _record_index_sql(sidecar, fork_path, kind, None)
        if surface == "record_index"
        else _presentation_key_sql(sidecar, fork_path, kind, None)
    )
    check_elected_key_unique(emit, relation_sql, surface, None, f"kind '{kind}'")
    rows = emit.query(relation_sql, ())
    return {str(row[0]): str(row[1]) for row in rows}


def _apply_output_columns(
    after: dict[str, object] | None,
    output_columns: "Sequence[tuple[str, str]]",
    key_value: str,
) -> dict[str, object] | None:
    """Rekey a raw after-image dict to its resolved output keys.

    The per-row analog of `presentation.resolve_stream_output_columns` /
    `resolve_membership_output_columns`: the naming authority's one consumer
    at render time. Replaces the shipped identity re-key/presentation_id-
    absorb rule and applies `rename` in the same pass — the identity entry's
    value is always the row's own elected key_value, regardless of the raw
    dict's 'record_id' entry, since a resolver's identity pair's fold-column
    name is always 'record_id'.

    Args:
        after: The raw after-image dict, keyed by fold column name
            (record_id, presentation_id when carried, prop__/elem__/member__
            columns), or None on a delete.
        output_columns: The resolved (fold column, output key) pairs.
        key_value: The row's elected identity value.

    Returns:
        The output-keyed after-image, or None when `after` is None.
    """
    if after is None:
        return None
    return {
        output_key: key_value if fold_column == "record_id" else after[fold_column]
        for fold_column, output_key in output_columns
    }


def _apply_kind_vocabulary_to_member_fields(
    after: dict[str, object],
    reference_fields: frozenset[str],
    kind_vocabulary: "Mapping[str, str]",
) -> dict[str, object]:
    """Map every reference field's member__<f>__kind value through the vocabulary.

    Applied before output-key renaming (the value-election attach point),
    so `apply_kind_vocabulary`'s identity fall-through sees the raw kind
    name. `rename` and `kind_labels` therefore never interact.

    Args:
        after: The after-image dict (post owner-identity-rekey / reference
            translation), keyed by fold column names.
        reference_fields: The stream's selected reference-valued field names.
        kind_vocabulary: The resolved config-level kind -> label mapping.

    Returns:
        `after` with every reference field's `member__<f>__kind` entry
        mapped through `apply_kind_vocabulary`; unchanged when
        `reference_fields` is empty.
    """
    if not reference_fields:
        return after
    result = dict(after)
    for field in reference_fields:
        kind_col = f"member__{field}__kind"
        value = result.get(kind_col)
        if value is not None:
            result[kind_col] = apply_kind_vocabulary(str(value), kind_vocabulary)
    return result


def _resolve_target_identity(
    emit: "Emit",
    fork_path: str,
    sidecar: "Sidecar",
    election: "Election",
    target_kind: str,
    record_id: str,
    identity_index_cache: dict[tuple[str, str], dict[str, str]],
    subtype_index_cache: dict[str, dict[str, str]],
) -> str:
    """Translate one reference value through its target kind's elected surface.

    Resolves the target row's own population (a flat kind's single population,
    or a sub-typed kind's population via the records-spine discriminator —
    the shipped per-row mixed-election rule), then renders that population's
    elected surface value. Falls through to `record_id` verbatim when the
    resolved surface is 'record_id', or when a population's identity index has
    no entry for the value (a dangling reference — never fabricated).

    Args:
        emit: The open emit.
        fork_path: The sole branch, from `require_single_branch`.
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        target_kind: The reference's target kind.
        record_id: The raw target record_id to translate.
        identity_index_cache: Mutable (kind, surface) -> identity map cache,
            shared across a stream's render pass.
        subtype_index_cache: Mutable kind -> (record_id -> sub_type) cache,
            shared across a stream's render pass.

    Returns:
        The target's elected-surface value for `record_id`.
    """
    domain = sidecar.subtype_values(target_kind)
    if not domain:
        surface = election.surface_for(target_kind, None)
    else:
        if target_kind not in subtype_index_cache:
            subtype_index_cache[target_kind] = resolve_subtype_index(emit, target_kind)
        sub_type = subtype_index_cache[target_kind].get(record_id)
        surface = election.surface_for(target_kind, sub_type)
    if surface == "record_id":
        return record_id
    cache_key = (target_kind, surface)
    if cache_key not in identity_index_cache:
        identity_index_cache[cache_key] = build_elected_identity_index(
            emit, fork_path, target_kind, surface
        )
    return identity_index_cache[cache_key].get(record_id, record_id)


def _translate_reference_columns(
    after: dict[str, object] | None,
    reference_targets: dict[str, str],
    emit: "Emit",
    fork_path: str,
    sidecar: "Sidecar",
    election: "Election",
    identity_index_cache: dict[tuple[str, str], dict[str, str]],
    subtype_index_cache: dict[str, dict[str, str]],
) -> dict[str, object] | None:
    """Translate every reference-valued after-image entry to its target's surface.

    Args:
        after: The after-image dict (post-identity-rekey), or None.
        reference_targets: `prop__<p>` bare property name -> target kind, for
            the stream's selected reference properties.
        emit: The open emit.
        fork_path: The sole branch.
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        identity_index_cache: Mutable (kind, surface) -> identity map cache.
        subtype_index_cache: Mutable kind -> (record_id -> sub_type) cache.

    Returns:
        `after` with every reference-valued entry translated; None unchanged;
        `after` unchanged when `reference_targets` is empty. A NULL reference
        value is never translated (stays NULL).
    """
    if after is None or not reference_targets:
        return after
    result = dict(after)
    for prop, target_kind in reference_targets.items():
        column = f"prop__{prop}"
        raw_value = result.get(column)
        if raw_value is not None:
            result[column] = _resolve_target_identity(
                emit,
                fork_path,
                sidecar,
                election,
                target_kind,
                str(raw_value),
                identity_index_cache,
                subtype_index_cache,
            )
    return result


def _translate_membership_member_fields(
    after: dict[str, object],
    reference_fields: frozenset[str],
    emit: "Emit",
    fork_path: str,
    sidecar: "Sidecar",
    election: "Election",
    identity_index_cache: dict[tuple[str, str], dict[str, str]],
    subtype_index_cache: dict[str, dict[str, str]],
) -> dict[str, object]:
    """Translate every membership reference field to its member row's kind's surface.

    The target kind varies per row: it is read from the sibling
    `member__<f>__kind` entry the same after-image already carries (the
    junction-member analog of `_translate_reference_columns`).

    Args:
        after: The after-image dict (post owner-identity-rekey).
        reference_fields: The stream's selected reference-valued field names.
        emit: The open emit.
        fork_path: The sole branch.
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        identity_index_cache: Mutable (kind, surface) -> identity map cache.
        subtype_index_cache: Mutable kind -> (record_id -> sub_type) cache.

    Returns:
        `after` with every reference field's `member__<f>__id` entry
        translated; unchanged when `reference_fields` is empty.
    """
    if not reference_fields:
        return after
    result = dict(after)
    for field in reference_fields:
        kind_col = f"member__{field}__kind"
        id_col = f"member__{field}__id"
        target_kind = result.get(kind_col)
        raw_id = result.get(id_col)
        if target_kind is not None and raw_id is not None:
            result[id_col] = _resolve_target_identity(
                emit,
                fork_path,
                sidecar,
                election,
                str(target_kind),
                str(raw_id),
                identity_index_cache,
                subtype_index_cache,
            )
    return result


def _validate_stream_naming(
    sidecar: "Sidecar",
    config: "StreamConfig",
    surface_by_stream: dict[str, "KeySurface"],
) -> None:
    """Run the naming eager gates (StreamRenameUnresolvable,
    StreamOutputNameCollision) over every declared stream.

    Recomputes each stream's output-column resolution — a pure function of
    (sidecar, stream, surface) — purely for its gate side effects; the
    result is discarded here and recomputed again at render time (the
    `resolve_stream_surfaces` recompute-not-thread posture, extended to
    naming).

    Args:
        sidecar: The open emit's sidecar view.
        config: The validated streaming configuration.
        surface_by_stream: Every stream's gated uniform elected surface.

    Raises:
        StreamRenameUnresolvable: A rename key names no selected property /
            field of its stream. Message leads with the stream name.
        StreamOutputNameCollision: Two of a stream's output keys collide, or
            one collides with a reserved name. Message leads with the
            stream name.
    """
    for stream in config.streams:
        identity_key = surface_by_stream[stream.name]
        try:
            if isinstance(stream, KindStream):
                resolve_stream_output_columns(
                    sidecar,
                    stream.kind,
                    stream.properties,
                    stream.rename,
                    identity_key,
                )
            else:
                resolve_membership_output_columns(
                    sidecar,
                    stream.membership,
                    stream.fields,
                    stream.rename,
                    identity_key,
                )
        except (StreamRenameUnresolvable, StreamOutputNameCollision) as exc:
            raise type(exc)(f"stream '{stream.name}': {exc}") from exc


def _validate_streams(
    emit: "Emit", config: "StreamConfig", notice_sink: "NoticeSink"
) -> tuple[
    str,
    "Election",
    dict[str, "KeySurface"],
    "Mapping[str, str]",
    dict[str, "frozenset[str] | None"],
]:
    """Run the eager business-rule validation pass over every declared stream.

    Checks the single-branch guard, then each stream's rules, in declaration
    order: kind-shaped streams run StreamKindResolvable /
    StreamSubTypesRequireSubtyping / StreamSubTypesDeclared /
    StreamPropertyResolvable / StreamPropertySliceOnly /
    StreamChangeScopeUnresolvable (`only` / `ignore`); membership-shaped
    streams run MembershipResolvable / MembershipFieldResolvable / the owner
    `sub_types` gate; then every stream resolves its row selection
    (`resolve_stream_selection`, the `StreamWhere*` gates, the per-element
    out-of-domain notice through `notice_sink`). Then resolves the election
    and runs the identity and edge gates (`resolve_stream_surfaces`, ranging
    a membership stream's uniformity gate over its addressed owner
    population set), the naming gates (`_validate_stream_naming`), and the
    kind-vocabulary gates (`resolve_stream_kind_vocabulary`).

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        notice_sink: The caller-supplied sink the out-of-domain `where`-value
            notices flow through.

    Returns:
        (fork_path, election, surface_by_stream, kind_vocabulary,
        selection_by_stream) — the resolved fork_path, the resolved
        election, every stream's gated uniform surface, the resolved
        config-level kind -> label map, and every stream's resolved
        selection set (None = no selection this device narrows).

    Raises:
        ExportError: The single-branch guard fails, or any per-stream business
            rule fails — message leading with the offending stream's name.
        TemporalClassUnavailableError: Propagated from the slice_only check.
        StreamWhereNotConstant, StreamWhereOnDiscriminator,
            StreamWhereColumnUnresolved, StreamWhereValueUncastable: A
            stream's `where` resolution fails.
        StreamChangeScopeUnresolvable: A stream's `only` / `ignore` entry
            names no prop__ column of the stream's kind.
        ElectionKindUnknown: A `keys` entry names no declared records kind.
        ElectionSubTypeUnknown: A `keys` map key is outside the kind's
            discriminator domain, or addresses a flat kind.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry.
        ElectionMixedIdentity: A stream's spanned populations elect differing
            surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election's spanned key
            spaces, or an edge's admitted target key spaces, contain a
            pairwise-unsafe pair.
        StreamRenameUnresolvable: A stream's rename key names no selected
            property / field.
        StreamOutputNameCollision: Two of a stream's output keys collide, or
            one collides with a reserved name.
        StreamKindLabelUnknown: A `kind_labels` key names no sidecar kind.
        StreamKindLabelCollision: A label or a per-stream `kind_label`
            equals a different kind's rendered name.
    """
    fork_path = require_single_branch(emit.sidecar)
    sidecar = emit.sidecar
    selection_by_stream: dict[str, "frozenset[str] | None"] = {}
    for stream in config.streams:
        if isinstance(stream, KindStream):
            _validate_kind_stream(sidecar, stream)
        else:
            _validate_membership_stream(sidecar, stream)
        selection_by_stream[stream.name] = resolve_stream_selection(
            emit, stream, notice_sink
        )
    election = resolve_election(sidecar, config.keys)
    surface_by_stream = resolve_stream_surfaces(sidecar, election, config)
    _validate_stream_naming(sidecar, config, surface_by_stream)
    kind_vocabulary = resolve_stream_kind_vocabulary(config, sidecar)
    return fork_path, election, surface_by_stream, kind_vocabulary, selection_by_stream


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


def _kind_audited_property_names(sidecar: "Sidecar", kind: str) -> frozenset[str]:
    """Every audited (tracked- or constant-class) property name on kind's
    records table.

    The default change-scope set for a kind-shaped stream's fold (design doc
    § Change scope): every `prop__` column minus the non-exempt slice_only
    population — history-untracked, contributing no change points, so
    excluding it leaves the narrowed default's event set byte-identical to
    the shipped full-property-set invocation. Read directly off the sidecar
    — a full-population fact, not an author-declared value; `only` / `ignore`
    narrow it further per the declaring stream.

    Args:
        sidecar: The open emit's sidecar view.
        kind: The record kind.

    Returns:
        Every prop__<p> column's bare name on records__<kind> whose class is
        not non-exempt slice_only.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        TemporalClassUnavailableError: Propagated from the slice_only check.
    """
    cols = sidecar.columns(f"records__{kind}")
    return frozenset(
        col.name[len("prop__") :]
        for col in cols
        if col.name.startswith("prop__")
        and not is_non_exempt_slice_only(sidecar, kind, col.name)
    )


def _resolve_kind_change_scope(
    audited: "frozenset[str]",
    only: "Sequence[str] | None",
    ignore: "Sequence[str] | None",
) -> frozenset[str]:
    """Narrow a kind-shaped stream's audited change scope by `only` / `ignore`.

    Args:
        audited: The kind's audited property set
            (`_kind_audited_property_names`).
        only: The stream's declared `only` entries, or None.
        ignore: The stream's declared `ignore` entries, or None.

    Returns:
        `only` verbatim (as a set) when declared; `audited` minus `ignore`
        when declared; `audited` otherwise — today's byte-identical default
        (design doc § Change scope).
    """
    if only is not None:
        return frozenset(only)
    if ignore is not None:
        return audited - frozenset(ignore)
    return audited


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


def _filter_rows_by_selection(
    rows: list[tuple[object, ...]],
    selected_ids: "frozenset[str] | None",
) -> list[tuple[object, ...]]:
    """Drop fold rows outside a stream's resolved `where` / owner selection.

    `resolve_stream_selection`'s satisfying record set (owner set, for a
    membership stream) — a kind stream's `where`-narrowed record set, or a
    membership stream's `sub_types` + `where`-narrowed owner set (design
    doc § Row selection). Dropped rows consume no `seq`, exactly as
    `_filter_rows_by_types`' shipped sub_types drop does; the two devices
    compose independently on a kind stream.

    Args:
        rows: Materialized fold rows for one stream (record_id first,
            owner record_id for a membership fold).
        selected_ids: The stream's resolved selection set, or None when the
            stream declares no selection this device narrows (every row
            stays in scope).

    Returns:
        `rows` unchanged when `selected_ids` is None; otherwise only the
        rows whose record_id is a member.
    """
    if selected_ids is None:
        return rows
    return [row for row in rows if str(row[_IDX_RECORD_ID]) in selected_ids]


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


def _wrap_stream_render_sql(
    fold_sql: str,
    col_names: "Sequence[str]",
    render: "Mapping[str, DecimalElection | JsonPrecisionElection] | None",
    prefix: str,
    stream_name: str,
) -> str:
    """Wrap a fold's SQL with the stream's numeric render elections, applied
    at the codec seam (design doc § Streaming attach): the post-fold SELECT
    that assembles after-images, upstream of `_build_after_image` /
    `_build_membership_after_image`. An elected column's codec-VARCHAR value
    renders through the shared decimal/json_precision authorities in place;
    every other column passes through verbatim. `render` empty/None ->
    `fold_sql` unchanged (byte-identical to today).

    Args:
        fold_sql: The fold's own SELECT (row-state-events or
            membership-events); its after-image columns are codec VARCHAR.
        col_names: The fold row's column names, in emission order (the fixed
            4-column prefix, then the after-image columns).
        render: The stream's resolved render map (bare name -> election), or
            None/empty.
        prefix: The after-image column prefix a render key addresses
            (`prop__` for a kind stream, `elem__` for a membership stream).
        stream_name: The declaring stream's name, for guard attribution.

    Returns:
        The wrapping SELECT, or `fold_sql` unchanged when `render` is empty.
    """
    if not render:
        return fold_sql
    alias = "_render"
    select_parts: list[str] = []
    for name in col_names:
        qualified = f'"{alias}"."{name}"'
        bare = name[len(prefix) :] if name.startswith(prefix) else None
        election = render.get(bare) if bare is not None else None
        if election is None:
            select_parts.append(f'{qualified} AS "{name}"')
        elif isinstance(election, DecimalElection):
            precision, scale = election.decimal
            double_expr = f"CAST({qualified} AS DOUBLE)"
            decimal_expr = render_decimal_expr(
                double_expr, precision, scale, name, stream_name
            )
            select_parts.append(f'CAST({decimal_expr} AS VARCHAR) AS "{name}"')
        else:
            json_expr = render_json_precision_expr(
                qualified, election.json_precision, name, stream_name
            )
            select_parts.append(f'{json_expr} AS "{name}"')
    select_list = ", ".join(select_parts)
    return f'SELECT {select_list} FROM ({fold_sql}) AS "{alias}"'


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
    election: "Election",
    surface_by_stream: dict[str, "KeySurface"],
    kind_vocabulary: "Mapping[str, str]",
    selection_by_stream: dict[str, "frozenset[str] | None"],
) -> Iterator[StreamEvent]:
    """Materialize one fold per kind-shaped stream, merge, and yield StreamEvents.

    Called only after the eager validation pass in iter_stream_events has
    succeeded, for content='state-changes'. Each stream's fold runs with
    change_scope = its `only` / audited-minus-`ignore` / audited change
    scope (§ Change scope — payload-independent of the stream's own
    `properties` selection, `properties` remains the fold's projection) and
    projection = the stream's declared properties; a stream that
    scopes an explicit sub_types subset is filtered post-fold via the
    discriminator index, and a stream with a `where` selection is further
    filtered post-fold via `selection_by_stream` (§ Row selection) — the two
    drop devices compose independently. Renders the elected key and
    after-image identity through `surface_by_stream` and `election` (§
    Message-key election); absent `keys`, every surface is 'record_id' and
    rendering is unchanged. The after-image's payload keys and the envelope
    `kind` resolve through `kind_vocabulary` and each stream's own `rename` /
    `kind_label` (§ Output-name resolution, § Kind vocabulary); route_table
    is unaffected.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).
        sidecar: The open emit's sidecar view.
        subtype_indexes: Per-sub-typed-kind record_id -> sub_type indexes;
            reused and extended as the reference-target identity cache.
        election: The resolved election.
        surface_by_stream: Every stream's gated uniform elected surface.
        kind_vocabulary: The resolved config-level kind -> label mapping.
        selection_by_stream: Every stream's resolved `where` selection set
            (None = no selection this device narrows).

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    stream_rows: list[list[_MergeRow]] = []
    col_names_by_stream: dict[str, list[str]] = {}
    kind_by_stream: dict[str, str] = {}
    reference_targets_by_stream: dict[str, dict[str, str]] = {}
    output_columns_by_stream: dict[str, list[tuple[str, str]]] = {}
    envelope_kind_by_stream: dict[str, str] = {}
    identity_index_cache: dict[tuple[str, str], dict[str, str]] = {}
    known_kinds = frozenset(known_records_kinds(sidecar))

    for stream in config.streams:
        assert isinstance(stream, KindStream)
        kind = stream.kind
        properties = frozenset(stream.properties)
        audited = _kind_audited_property_names(sidecar, kind)
        change_scope = _resolve_kind_change_scope(audited, stream.only, stream.ignore)

        col_names = record_fold_row_column_names(sidecar, kind, properties)
        sql = build_row_state_events_sql(
            sidecar, fork_path, kind, properties, change_scope=change_scope
        )
        sql = _wrap_stream_render_sql(
            sql, col_names, stream.render, "prop__", stream.name
        )
        rows = emit.query(sql, ())

        if stream.sub_types is not None and kind in subtype_indexes:
            rows = _filter_rows_by_types(rows, stream.sub_types, subtype_indexes[kind])
        rows = _filter_rows_by_selection(rows, selection_by_stream[stream.name])

        stream_rows.append(_rows_to_keyed(rows, stream.name))
        col_names_by_stream[stream.name] = col_names
        kind_by_stream[stream.name] = kind
        reference_targets_by_stream[stream.name] = kind_reference_targets(
            sidecar, kind, stream.properties, known_kinds
        )
        output_columns_by_stream[stream.name] = resolve_stream_output_columns(
            sidecar,
            kind,
            stream.properties,
            stream.rename,
            surface_by_stream[stream.name],
        )
        envelope_kind_by_stream[stream.name] = resolve_stream_envelope_kind(
            stream.kind_label, kind_vocabulary, kind
        )

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

        surface = surface_by_stream[stream_name]
        if surface == "record_id":
            key_value = record_id
        else:
            cache_key = (kind, surface)
            if cache_key not in identity_index_cache:
                identity_index_cache[cache_key] = build_elected_identity_index(
                    emit, fork_path, kind, surface
                )
            key_value = identity_index_cache[cache_key].get(record_id, record_id)

        after = _build_after_image(row, col_names, op)
        after = _translate_reference_columns(
            after,
            reference_targets_by_stream[stream_name],
            emit,
            fork_path,
            sidecar,
            election,
            identity_index_cache,
            subtype_indexes,
        )
        after = _apply_output_columns(
            after, output_columns_by_stream[stream_name], key_value
        )
        ts = render_ts(event_sim_time, anchor)

        sub_type: str | None = None
        kind_is_subtyped = _is_kind_subtyped(kind, sidecar)
        if kind_is_subtyped and kind in subtype_indexes:
            sub_type = subtype_indexes[kind].get(record_id)
        attrs = route_attributes(kind_is_subtyped, kind, sub_type)

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=envelope_kind_by_stream[stream_name],
            record_id=record_id,
            presentation_id=presentation_id,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=stream_name,
            route_table=attrs["route_table"],
            key_column=surface,
            key_value=key_value,
        )


def _iter_membership_streams_inner(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    fork_path: str,
    sidecar: "Sidecar",
    election: "Election",
    surface_by_stream: dict[str, "KeySurface"],
    kind_vocabulary: "Mapping[str, str]",
    selection_by_stream: dict[str, "frozenset[str] | None"],
) -> Iterator[StreamEvent]:
    """Materialize one fold per membership-shaped stream, merge, and yield events.

    Called only after the eager validation pass in iter_stream_events has
    succeeded, for content='membership-events'. A stream's owner `sub_types`
    / `where` selection is filtered post-fold via `selection_by_stream` (§
    Row selection) — every `join`/`leave` of a non-satisfying owner's
    collection is dropped. Renders the owner's elected key and after-image
    identity, and translates every reference-valued member field to its
    member row's own kind's elected surface, through `surface_by_stream` and
    `election` (§ Message-key election); absent `keys`, every surface is
    'record_id' and rendering is unchanged. The after-image's payload keys,
    its `<f>_kind` values, and the envelope `kind` resolve through
    `kind_vocabulary` and each stream's own `rename` / `kind_label` (§
    Output-name resolution, § Kind vocabulary); route_table is unaffected.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None for raw-ns timestamps.
        fork_path: The sole branch fork_path (from require_single_branch).
        sidecar: The open emit's sidecar view.
        election: The resolved election.
        surface_by_stream: Every stream's gated uniform owner elected surface.
        kind_vocabulary: The resolved config-level kind -> label mapping.
        selection_by_stream: Every stream's resolved owner `sub_types` /
            `where` selection set (None = no selection this device narrows).

    Returns:
        An iterator of StreamEvent in global seq order.
    """
    stream_rows: list[list[_MergeRow]] = []
    col_names_by_stream: dict[str, list[str]] = {}
    owner_kind_by_stream: dict[str, str] = {}
    property_by_stream: dict[str, str] = {}
    reference_fields_by_stream: dict[str, frozenset[str]] = {}
    output_columns_by_stream: dict[str, list[tuple[str, str]]] = {}
    envelope_kind_by_stream: dict[str, str] = {}
    identity_index_cache: dict[tuple[str, str], dict[str, str]] = {}
    subtype_index_cache: dict[str, dict[str, str]] = {}

    for stream in config.streams:
        assert isinstance(stream, MembershipStream)
        owner_kind = stream.membership.kind
        property_name = stream.membership.property

        col_names = membership_fold_row_column_names(
            sidecar, owner_kind, property_name, stream.fields
        )
        sql = build_membership_events_sql(
            sidecar, fork_path, owner_kind, property_name, stream.fields
        )
        sql = _wrap_stream_render_sql(
            sql, col_names, stream.render, "elem__", stream.name
        )
        rows = emit.query(sql, ())
        rows = _filter_rows_by_selection(rows, selection_by_stream[stream.name])

        stream_rows.append(_rows_to_keyed(rows, stream.name))
        col_names_by_stream[stream.name] = col_names
        owner_kind_by_stream[stream.name] = owner_kind
        property_by_stream[stream.name] = property_name
        reference_fields_by_stream[stream.name] = membership_reference_fields(
            sidecar, owner_kind, property_name, stream.fields
        )
        output_columns_by_stream[stream.name] = resolve_membership_output_columns(
            sidecar,
            stream.membership,
            stream.fields,
            stream.rename,
            surface_by_stream[stream.name],
        )
        envelope_kind_by_stream[stream.name] = resolve_stream_envelope_kind(
            stream.kind_label, kind_vocabulary, owner_kind
        )

    seq = 0
    for _merge_key, row, stream_name in heapq.merge(*stream_rows, key=lambda x: x[0]):
        seq += 1

        op = str(row[_IDX_OP])
        record_id = str(row[_IDX_RECORD_ID])
        event_sim_time = cast(int, row[_IDX_EVENT_SIM_TIME])
        owner_kind = owner_kind_by_stream[stream_name]
        property_name = property_by_stream[stream_name]

        col_names = col_names_by_stream[stream_name]

        surface = surface_by_stream[stream_name]
        if surface == "record_id":
            key_value = record_id
        else:
            cache_key = (owner_kind, surface)
            if cache_key not in identity_index_cache:
                identity_index_cache[cache_key] = build_elected_identity_index(
                    emit, fork_path, owner_kind, surface
                )
            key_value = identity_index_cache[cache_key].get(record_id, record_id)

        after = _build_membership_after_image(row, col_names)
        reference_fields = reference_fields_by_stream[stream_name]
        after = _translate_membership_member_fields(
            after,
            reference_fields,
            emit,
            fork_path,
            sidecar,
            election,
            identity_index_cache,
            subtype_index_cache,
        )
        after = _apply_kind_vocabulary_to_member_fields(
            after, reference_fields, kind_vocabulary
        )
        after = cast(
            "dict[str, object]",
            _apply_output_columns(
                after, output_columns_by_stream[stream_name], key_value
            ),
        )
        ts = render_ts(event_sim_time, anchor)

        attrs = membership_route_attributes(owner_kind, property_name)

        yield StreamEvent(
            seq=seq,
            op=op,  # type: ignore[arg-type]
            kind=envelope_kind_by_stream[stream_name],
            record_id=record_id,
            presentation_id=None,
            event_sim_time=event_sim_time,
            ts=ts,
            after=after,
            topic=stream_name,
            route_table=attrs["route_table"],
            key_column=surface,
            key_value=key_value,
        )


def iter_stream_events(
    emit: "Emit",
    config: "StreamConfig",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> Iterator[StreamEvent]:
    """Yield the stream's events in canonical total order with seq stamped.

    Split into two phases: an eager validation pass that runs at call time
    (before the first next()), then an inner generator that materializes one
    fold per declared stream (kind-shaped: change scope = the kind's full
    property set, projection = the stream's declared properties;
    membership-shaped: unchanged), drops rows outside the stream's sub_types
    scope post-fold via the discriminator index, drops rows outside the
    stream's resolved `where` / owner selection post-fold (§ Row selection),
    k-way-merges under (event_sim_time, event_class, stream_name, record_id),
    stamps seq, renders ts, stamps topic = the declaring stream's name and
    route_table = the per-event leaf, and yields StreamEvents.

    See § Ordering and `seq`, § Timestamp rendering, § Business Rules,
    § Message-key election.

    Args:
        emit: The open emit (reader + connection).
        config: The validated streaming configuration.
        anchor: The resolved effective anchor, or None.
        notice_sink: The caller-supplied notice receiver (required — a caller
            wanting silence passes a discarding sink).

    Returns:
        An iterator of StreamEvent in global seq order.

    Raises:
        ExportError: An eager business rule failed — message leads with the
            offending stream's name. Raised at call time, before the first
            next().
        TemporalClassUnavailableError: Propagated from the slice_only check.
        ElectionKindUnknown: A `keys` entry names no declared records kind.
        ElectionSubTypeUnknown: A `keys` map key is outside the kind's
            discriminator domain, or addresses a flat kind.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry.
        ElectionMixedIdentity: A stream's spanned populations elect differing
            surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election's spanned key
            spaces, or an edge's admitted target key spaces, contain a
            pairwise-unsafe pair.
        StreamRenameUnresolvable: A stream's rename key names no selected
            property / field.
        StreamOutputNameCollision: Two of a stream's output keys collide, or
            one collides with a reserved name.
        StreamKindLabelUnknown: A `kind_labels` key names no sidecar kind.
        StreamKindLabelCollision: A label or a per-stream `kind_label`
            equals a different kind's rendered name.
        StreamWhereNotConstant, StreamWhereOnDiscriminator,
            StreamWhereColumnUnresolved, StreamWhereValueUncastable: A
            stream's `where` resolution fails.
    """
    fork_path, election, surface_by_stream, kind_vocabulary, selection_by_stream = (
        _validate_streams(emit, config, notice_sink)
    )
    sidecar = emit.sidecar

    if config.content == "membership-events":
        return _iter_membership_streams_inner(
            emit,
            config,
            anchor,
            fork_path,
            sidecar,
            election,
            surface_by_stream,
            kind_vocabulary,
            selection_by_stream,
        )

    subtype_indexes = _build_subtype_indexes(emit, config, sidecar)
    return _iter_kind_streams_inner(
        emit,
        config,
        anchor,
        fork_path,
        sidecar,
        subtype_indexes,
        election,
        surface_by_stream,
        kind_vocabulary,
        selection_by_stream,
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
