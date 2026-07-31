"""Event-log render: the polymorphic audit table (`exporters/source/events.py`).

The one render that composes two folds (`build_row_state_events_sql`,
`build_membership_events_sql`) plus lag/diff/JSON machinery no other render
shares — warrants its own module (`docs/sprints/source-declared-tables/spec.md`
§ 3c). `SourceEventSourcePlan` / `SourceEventLogPlan` are hand-constructed in
tests during this phase; a later phase wires `plan.py` to produce them and the
engine to compile them.

**`change_edges.source_column` convention** (this module's choice, since the
plan is hand-constructed here): a records source's audited reference-valued
property uses `prop__<p>` (matching `exporters.source.plan`'s
`_resolve_reference_prop_edges`); a membership source's audited member
reference field uses `member__<f>__id` (matching
`_resolve_member_field_edge`) — the same source-column identities a future
plan-builder phase would derive by calling those same functions.

Layer-direction invariant: imports the reader (through the derivations
layer), the derivations layer's two event folds, the mode-neutral election
module (`build_identity_translation_sql`, `build_population_spine_sql`),
`fabulexa_forge.anchor`, `fabulexa_forge._sql`, the sibling `source.plan`
module (`SourceEdgeSurface`, TYPE_CHECKING only), `exporters.populations`
(`Population`, TYPE_CHECKING only), config.models (`KeySurface`, TYPE_CHECKING
only), and stdlib. Never imports `exporters.dimensional.*` or
`exporters.streaming.*`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.populations import Population
    from fabulexa_forge.exporters.source.plan import SourceEdgeSurface
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.membership_events import build_membership_events_sql
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.exporters.election import (
    build_identity_translation_sql,
    build_population_spine_sql,
)

_PROP_PREFIX = "prop__"


@dataclass(frozen=True)
class SourceEventSourcePlan:
    """One resolved audited population set of the event log.

    Lives in `exporters/source/events.py` (Phase 2 hand-constructs it in
    tests; a later phase's plan builder produces it).
    """

    item_type: str
    """The contract identity: the kind name for a records source,
    '<K>.<property>' for a membership source."""
    kind: str
    """The audited kind (records source) or the owner kind `<K>`
    (membership source)."""
    property: "str | None"
    """The membership property, or None for a records source."""
    populations: "tuple[Population, ...]"
    """Records source: the addressed atoms (drives the fold's per-row
    discriminator narrowing and the overlap check). Membership source: the
    owner kind's full declared domain (drives per-row item_id resolution;
    membership sources are disjoint by (kind, property), not by these)."""
    audited_properties: "tuple[str, ...]"
    """The audited set, bare names, sidecar column-declaration order:
    every tracked- and constant-class property (discriminator included,
    slice_only policy-omitted) narrowed by only / widened-by-subtraction
    via ignore, for a records source; the selected element-schema field
    names (member reference fields by bare field name — the pair expands
    at render) for a membership source. Feeds the folds' property set
    verbatim (`build_row_state_events_sql` / `build_membership_events_sql`)."""
    item_surface: "tuple[tuple[str | None, KeySurface], ...]"
    """Per-population elected surface of the item target — the audited
    kind's addressed populations (records source) or the owner kind's
    domain (membership source). Union-safety is gated at plan time per
    item-type over the union across sources sharing the item_type."""
    change_edges: "tuple[SourceEdgeSurface, ...]"
    """One entry per audited reference-valued property (records source)
    and per audited member reference field (membership source) whose
    target carries a declared records table — the elected rendering
    inside `changes`, gated per audited reference property. `source_column`
    is `prop__<p>` for a records source, `member__<f>__id` for a membership
    source (this module's § docstring convention)."""


@dataclass(frozen=True)
class SourceEventLogPlan:
    """The resolved event log: one polymorphic audit table."""

    name: str
    """Author-verbatim output table name."""
    sources: "tuple[SourceEventSourcePlan, ...]"
    """Declaration order; population sets pairwise-disjoint (validated by
    the plan builder, not by this render)."""
    item_id_type: str
    """The junction-member-column type rule's verdict over the union of
    every source's `item_surface`: the common declared type when all
    agree, else 'VARCHAR' (record_index digit-rendered)."""


# ---------------------------------------------------------------------------
# `changes` JSON object construction
# ---------------------------------------------------------------------------


def _json_key_literal(key: str) -> str:
    """The SQL string literal for one JSON object key, pre-escaped.

    Keys are compile-time Python strings (audited property / membership
    field bare names), so escaping happens once in Python via `json.dumps`
    rather than at SQL-evaluation time.

    Args:
        key: The bare JSON key.

    Returns:
        A SQL string literal rendering `"<escaped-key>"` (JSON-quoted).
    """
    return _sql_literal(json.dumps(key))


def _json_value_sql(value_expr: str) -> str:
    """The runtime JSON-encoded rendering of one VARCHAR value expression.

    Uses DuckDB's `to_json` for proper JSON string escaping (quotes,
    backslashes, control characters); `to_json(NULL)` returns SQL NULL, so
    it is coalesced to the JSON `null` token.

    Args:
        value_expr: A VARCHAR-typed SQL expression (a fold after-image
            value, translated or verbatim).

    Returns:
        A VARCHAR SQL expression rendering the JSON-encoded value.
    """
    return f"COALESCE(to_json({value_expr}), 'null')"


def _json_pair_fragment_sql(key: str, old_value_expr: str, new_value_expr: str) -> str:
    """One `"<key>":[old,new]` JSON fragment, values JSON-encoded at runtime.

    Args:
        key: The bare JSON key (compile-time constant).
        old_value_expr: The VARCHAR SQL expression for the pair's old value.
        new_value_expr: The VARCHAR SQL expression for the pair's new value.

    Returns:
        A VARCHAR SQL expression rendering `"<key>":[<old>,<new>]`.
    """
    return (
        f"{_json_key_literal(key)} || ':[' || {_json_value_sql(old_value_expr)}"
        f" || ',' || {_json_value_sql(new_value_expr)} || ']'"
    )


def build_changes_object_expr(
    entries: "tuple[tuple[str, str, str], ...]",
) -> str:
    """The deterministic JSON-object SQL expression for one `changes` cell.

    Mode-owned SQL: builds a VARCHAR expression rendering
    `{"<key>": [old, new], ...}` from (key, old_value_expr, new_value_expr)
    triples, in the given order — entry inclusion (the update diff, the
    suppressed no-change row) is the caller's WHERE/CASE concern; this owns
    only object construction: JSON string escaping of keys and of the
    VARCHAR value expressions, `null` for SQL NULL, `{}` for an empty
    tuple. Never the conformance codec.

    Args:
        entries: (JSON key, old-value SQL expr, new-value SQL expr)
            triples, output order. Value exprs are VARCHAR-typed (the
            folds' after-image strings, already elected-translated).

    Returns:
        A VARCHAR-typed SQL expression.
    """
    if not entries:
        return "'{}'"
    fragments = [
        _json_pair_fragment_sql(key, old_expr, new_expr)
        for key, old_expr, new_expr in entries
    ]
    return "'{' || " + " || ',' || ".join(fragments) + " || '}'"


def _build_diff_changes_expr(entries: "tuple[tuple[str, str, str], ...]") -> str:
    """The update-diff `changes` expression: only entries whose old value
    differs from its new value, in the given order.

    The caller-side diff logic `build_changes_object_expr`'s docstring
    disclaims: per entry, a CASE evaluates to the fragment or SQL NULL when
    old and new are not distinct, and the surviving fragments are filtered
    (`list_filter`) and comma-joined (`array_to_string`).

    Args:
        entries: (JSON key, old-value SQL expr, new-value SQL expr)
            triples, output order.

    Returns:
        A VARCHAR-typed SQL expression; `'{}'` when every entry is
        unchanged (or `entries` is empty).
    """
    if not entries:
        return "'{}'"
    cases = [
        f"CASE WHEN ({old_expr}) IS NOT DISTINCT FROM ({new_expr}) THEN NULL"
        f" ELSE {_json_pair_fragment_sql(key, old_expr, new_expr)} END"
        for key, old_expr, new_expr in entries
    ]
    array_sql = "[" + ", ".join(cases) + "]"
    filtered_sql = f"list_filter({array_sql}, x -> x IS NOT NULL)"
    return f"'{{' || COALESCE(array_to_string({filtered_sql}, ','), '') || '}}'"


# ---------------------------------------------------------------------------
# Identity translation joins
# ---------------------------------------------------------------------------


def _edge_translation_join(
    sidecar: "Sidecar",
    fork_path: str,
    edge: "SourceEdgeSurface",
    alias: str,
    id_expr: str,
    kind_expr: "str | None",
) -> "tuple[str, str]":
    """One change-edge's LEFT JOIN clause and translated value expression.

    A single-target-kind edge (a records-source audited reference
    property) composes one `build_identity_translation_sql` relation,
    keyed on `id_expr`. A multi-target-kind edge (a membership member
    reference field) unions one relation per admitted kind, keyed on
    `(kind_expr, id_expr)` — the per-row device `<f>_kind` disambiguates.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        edge: The resolved change edge.
        alias: The join's SQL alias, unique within the arm.
        id_expr: The qualified raw record-id-valued expression on the
            arm's own relation to join against.
        kind_expr: The qualified `member__<f>__kind` expression, for a
            multi-target-kind edge; None for a single-target-kind edge.

    Returns:
        A 2-tuple: the LEFT JOIN clause (leading space), and the
        translated VARCHAR value expression.
    """
    if len(edge.target_kinds) == 1:
        kind, per_population = edge.per_kind_populations[0]
        rel_sql = build_identity_translation_sql(
            sidecar, fork_path, kind, per_population
        )
        join = (
            f' LEFT JOIN ({rel_sql}) AS "{alias}" ON {id_expr} = "{alias}"."record_id"'
        )
        return join, f'"{alias}"."elected_value"'

    assert kind_expr is not None, "a multi-target-kind edge needs the kind column"
    parts = []
    for kind, per_population in edge.per_kind_populations:
        rel_sql = build_identity_translation_sql(
            sidecar, fork_path, kind, per_population
        )
        parts.append(
            f'SELECT {_sql_literal(kind)} AS "kind", "record_id", "elected_value"'
            f' FROM ({rel_sql}) AS "_k"'
        )
    union_sql = " UNION ALL ".join(parts)
    join = (
        f' LEFT JOIN ({union_sql}) AS "{alias}"'
        f' ON {kind_expr} = "{alias}"."kind" AND {id_expr} = "{alias}"."record_id"'
    )
    return join, f'"{alias}"."elected_value"'


def _item_id_join_and_expr(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    item_surface: "tuple[tuple[str | None, KeySurface], ...]",
    item_id_type: str,
    id_expr: str,
) -> "tuple[str, str]":
    """The item-identity LEFT JOIN clause and (possibly CAST) `item_id` expr.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The item target kind (the audited kind, or the membership
            owner kind).
        item_surface: The item target's per-population elected surface.
        item_id_type: The log's resolved `item_id` column type.
        id_expr: The qualified raw record-id-valued expression on the
            arm's own relation to join against.

    Returns:
        A 2-tuple: the LEFT JOIN clause (leading space), and the
        `item_id` value expression (CAST to `item_id_type` when it is not
        `'VARCHAR'`).
    """
    rel_sql = build_identity_translation_sql(sidecar, fork_path, kind, item_surface)
    join = f' LEFT JOIN ({rel_sql}) AS "_item" ON {id_expr} = "_item"."record_id"'
    value_expr = '"_item"."elected_value"'
    if item_id_type != "VARCHAR":
        value_expr = f"CAST({value_expr} AS {item_id_type})"
    return join, value_expr


def _records_population_filter_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    populations: "tuple[Population, ...]",
) -> "str | None":
    """The records-source per-row population semi-join filter, or None.

    None when the kind is flat, or `populations` addresses its full
    declared domain — the full domain needs no restriction (mirrors
    `build_population_spine_sql`'s own precondition).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The audited kind.
        populations: The source's addressed populations.

    Returns:
        A complete `record_id`-producing SELECT (the population spine), or
        None when no restriction applies.
    """
    domain = sidecar.subtype_values(kind)
    if not domain:
        return None
    requested = tuple(p.sub_type for p in populations if p.sub_type is not None)
    if set(requested) == set(domain):
        return None
    return build_population_spine_sql(sidecar, fork_path, kind, requested)


# ---------------------------------------------------------------------------
# Records-source arm
# ---------------------------------------------------------------------------


def _build_records_arm_sql(
    sidecar: "Sidecar",
    fork_path: str,
    anchor: "EffectiveAnchor",
    source: "SourceEventSourcePlan",
    item_id_type: str,
) -> str:
    """One records source's UNION-ALL arm of the event-log render.

    Composes `build_row_state_events_sql`, narrowed per row to
    `source.populations` through the records-spine discriminator; recodes
    op c/u/d -> create/update/destroy. Old values are a per-record LAG over
    the fold's own audited after-images (translated first where a change
    edge applies); `changes` is the full object (`build_changes_object_expr`)
    for create/destroy rows, the differing-only object
    (`_build_diff_changes_expr`) for update rows — an update row touching no
    audited property is dropped. `item_id` joins the source's `item_surface`
    translation relation on the fold's own `record_id` (never the nulled
    after-image), so it is never NULL.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        anchor: The resolved wallclock anchor.
        source: The resolved records-source unit.
        item_id_type: The log's resolved `item_id` column type.

    Returns:
        A SELECT producing the arm's row shape (§ `build_event_log_sql`).
    """
    kind = source.kind
    properties = frozenset(source.audited_properties)
    fold_sql = build_row_state_events_sql(sidecar, fork_path, kind, properties)

    filter_sql = _records_population_filter_sql(
        sidecar, fork_path, kind, source.populations
    )
    narrowed_sql = (
        f'SELECT * FROM ({fold_sql}) AS "_f" WHERE "_f"."record_id" IN ({filter_sql})'
        if filter_sql is not None
        else fold_sql
    )

    edges_by_property = {
        edge.source_column[len(_PROP_PREFIX) :]: edge for edge in source.change_edges
    }

    item_join, item_id_expr = _item_id_join_and_expr(
        sidecar,
        fork_path,
        kind,
        source.item_surface,
        item_id_type,
        '"_narrowed"."record_id"',
    )
    joins = [item_join]

    value_selects = []
    for prop in source.audited_properties:
        raw_expr = f'"_narrowed"."{_PROP_PREFIX}{prop}"'
        edge = edges_by_property.get(prop)
        if edge is None:
            value_selects.append(f'{raw_expr} AS "_val__{prop}"')
            continue
        alias = f"_edge__{prop}"
        join, value_expr = _edge_translation_join(
            sidecar, fork_path, edge, alias, raw_expr, None
        )
        joins.append(join)
        value_selects.append(f'{value_expr} AS "_val__{prop}"')
    joins_sql = "".join(joins)

    valued_select = ", ".join(
        ['"_narrowed".*', f'{item_id_expr} AS "_item_id"', *value_selects]
    )
    valued_sql = (
        f'SELECT {valued_select} FROM ({narrowed_sql}) AS "_narrowed"{joins_sql}'
    )

    lag_selects = [
        (
            f'LAG("_valued"."_val__{prop}") OVER (PARTITION BY "_valued"."record_id"'
            f' ORDER BY "_valued"."event_sim_time") AS "_old__{prop}"'
        )
        for prop in source.audited_properties
    ]
    lagged_select = ", ".join(['"_valued".*', *lag_selects])
    lagged_sql = f'SELECT {lagged_select} FROM ({valued_sql}) AS "_valued"'

    entries = tuple(
        (prop, f'"_lagged"."_old__{prop}"', f'"_lagged"."_val__{prop}"')
        for prop in source.audited_properties
    )
    full_expr = build_changes_object_expr(entries)
    diff_expr = _build_diff_changes_expr(entries)
    changes_expr = (
        f'CASE WHEN "_lagged"."op" = \'u\' THEN {diff_expr} ELSE {full_expr} END'
    )
    event_expr = (
        "CASE \"_lagged\".\"op\" WHEN 'c' THEN 'create' WHEN 'u' THEN 'update'"
        " WHEN 'd' THEN 'destroy' END"
    )

    events_select = (
        f'"_lagged".*, {event_expr} AS "_event", {changes_expr} AS "_changes"'
    )
    events_sql = f'SELECT {events_select} FROM ({lagged_sql}) AS "_lagged"'

    occurred_at_expr = render_anchor_timestamp_expr(
        anchor, '"_events"."event_sim_time"', "occurred_at"
    )
    final_select = ", ".join(
        [
            f'{_sql_literal(source.item_type)} AS "item_type"',
            '"_events"."_item_id" AS "item_id"',
            '"_events"."_event" AS "event"',
            occurred_at_expr,
            '"_events"."_changes" AS "changes"',
            '"_events"."event_sim_time" AS "event_sim_time"',
            '"_events"."event_class" AS "event_class"',
            '"_events"."record_id" AS "_order_record_id"',
            'CAST(NULL AS VARCHAR) AS "_order_fields"',
        ]
    )
    return (
        f'SELECT {final_select} FROM ({events_sql}) AS "_events"'
        ' WHERE NOT ("_events"."_event" = \'update\' AND "_events"."_changes" = \'{}\')'
    )


# ---------------------------------------------------------------------------
# Membership-source arm
# ---------------------------------------------------------------------------


def _join_leave_old_new(value_expr: str) -> "tuple[str, str]":
    """A membership field's (old, new) pair from its own row's op.

    A membership row carries no history of its own, so old/new derive from
    the row's own `op`, not a lag: `[null, value]` on join, `[value, null]`
    on leave.

    Args:
        value_expr: The field's (possibly already-translated) value
            expression, read off the row regardless of op.

    Returns:
        (old-value SQL expr, new-value SQL expr).
    """
    old_expr = f'CASE WHEN "_fold"."op" = \'leave\' THEN {value_expr} ELSE NULL END'
    new_expr = f'CASE WHEN "_fold"."op" = \'join\' THEN {value_expr} ELSE NULL END'
    return old_expr, new_expr


def _membership_field_columns(
    sidecar: "Sidecar", table_name: str, field: str
) -> "tuple[str, ...]":
    """One membership field's payload column name(s): a reference pair or a
    scalar column.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The `membership__<K>__<p>` table.
        field: The bare element-schema field name.

    Returns:
        `(member__<f>__kind, member__<f>__id)` for a reference field,
        `(elem__<f>,)` for a scalar field.
    """
    names = {col.name for col in sidecar.columns(table_name)}
    kind_col = f"member__{field}__kind"
    id_col = f"member__{field}__id"
    if kind_col in names and id_col in names:
        return (kind_col, id_col)
    return (f"elem__{field}",)


def _membership_sort_key_expr(
    sidecar: "Sidecar", table_name: str, fields: "tuple[str, ...]"
) -> str:
    """The membership arm's synthesized field tie-break sort key.

    One VARCHAR column standing in for "membership fields in
    element-schema declaration order, VARCHAR-compared, NULLS FIRST" (the
    junction render's tie-break) across a UNION ALL whose branches may
    carry differing field arities: each field's value is
    NULLS-FIRST-encoded (a leading marker byte distinguishing NULL from any
    string) and the per-field encodings are joined by a delimiter byte, so
    lexicographic comparison of the combined key reproduces the per-field
    NULLS-FIRST comparison whenever it matters (rows sharing an
    `item_type` share one source, hence one field arity).

    Args:
        sidecar: The open emit's sidecar.
        table_name: The `membership__<K>__<p>` table.
        fields: The source's selected element-schema field names, order.

    Returns:
        A VARCHAR SQL expression, or `'NULL'` when `fields` is empty.
    """
    if not fields:
        return "NULL"
    parts = []
    for field in fields:
        for col in _membership_field_columns(sidecar, table_name, field):
            expr = f'"_fold"."{col}"'
            parts.append(
                f"(CASE WHEN {expr} IS NULL THEN chr(0) ELSE chr(1) || {expr} END)"
            )
    return " || chr(31) || ".join(parts)


def _build_membership_arm_sql(
    sidecar: "Sidecar",
    fork_path: str,
    anchor: "EffectiveAnchor",
    source: "SourceEventSourcePlan",
    item_id_type: str,
) -> str:
    """One membership source's UNION-ALL arm of the event-log render.

    Composes `build_membership_events_sql`; recodes op join/leave ->
    create/destroy. Every selected field's value lives on its own row (a
    membership row carries no history of its own), so old/new derive from
    the row's own op, not a lag: join -> `[null, value]`, leave ->
    `[value, null]`. `item_id` joins the owner's `item_surface` translation
    relation on the fold's own (already-owner-VARCHAR) `record_id`.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        anchor: The resolved wallclock anchor.
        source: The resolved membership-source unit.
        item_id_type: The log's resolved `item_id` column type.

    Returns:
        A SELECT producing the arm's row shape (§ `build_event_log_sql`).
    """
    owner_kind = source.kind
    property_name = source.property
    assert property_name is not None, "a membership source carries its property"
    fields = source.audited_properties
    fold_sql = build_membership_events_sql(
        sidecar, fork_path, owner_kind, property_name, fields
    )
    table_name = f"membership__{owner_kind}__{property_name}"

    edges_by_source_column = {edge.source_column: edge for edge in source.change_edges}

    item_join, item_id_expr = _item_id_join_and_expr(
        sidecar,
        fork_path,
        owner_kind,
        source.item_surface,
        item_id_type,
        '"_fold"."record_id"',
    )
    joins = [item_join]

    entries: list[tuple[str, str, str]] = []
    for field in fields:
        cols = _membership_field_columns(sidecar, table_name, field)
        if len(cols) == 1:
            raw_expr = f'"_fold"."{cols[0]}"'
            entries.append((field, *_join_leave_old_new(raw_expr)))
            continue

        kind_col, id_col = cols
        kind_expr = f'"_fold"."{kind_col}"'
        id_raw_expr = f'"_fold"."{id_col}"'
        edge = edges_by_source_column.get(id_col)
        if edge is None:
            id_value_expr = id_raw_expr
        else:
            alias = f"_edge__{field}"
            join, id_value_expr = _edge_translation_join(
                sidecar, fork_path, edge, alias, id_raw_expr, kind_expr
            )
            joins.append(join)
        entries.append((f"{field}_kind", *_join_leave_old_new(kind_expr)))
        entries.append((f"{field}_id", *_join_leave_old_new(id_value_expr)))

    joins_sql = "".join(joins)
    changes_expr = build_changes_object_expr(tuple(entries))
    event_expr = (
        "CASE \"_fold\".\"op\" WHEN 'join' THEN 'create'"
        " WHEN 'leave' THEN 'destroy' END"
    )
    occurred_at_expr = render_anchor_timestamp_expr(
        anchor, '"_fold"."event_sim_time"', "occurred_at"
    )
    order_fields_expr = _membership_sort_key_expr(sidecar, table_name, fields)

    final_select = ", ".join(
        [
            f'{_sql_literal(source.item_type)} AS "item_type"',
            f'{item_id_expr} AS "item_id"',
            f'{event_expr} AS "event"',
            occurred_at_expr,
            f'{changes_expr} AS "changes"',
            '"_fold"."event_sim_time" AS "event_sim_time"',
            '"_fold"."event_class" AS "event_class"',
            '"_fold"."record_id" AS "_order_record_id"',
            f'{order_fields_expr} AS "_order_fields"',
        ]
    )
    return f'SELECT {final_select} FROM ({fold_sql}) AS "_fold"{joins_sql}'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_event_log_sql(
    sidecar: "Sidecar",
    fork_path: str,
    log: "SourceEventLogPlan",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """The polymorphic event-log render: one audit table, event grain.

    Per records source: composes `build_row_state_events_sql(sidecar,
    fork_path, kind, frozenset(audited_properties))`, narrowed per row to
    the source's populations through the records-spine discriminator;
    recodes op c/u/d -> create/update/destroy. Per membership source:
    composes `build_membership_events_sql(sidecar, fork_path, owner_kind,
    property, fields)` (join -> create, leave -> destroy). Old values are
    a per-record lag over the fold's own audited after-images; `changes`
    is the design-doc JSON changeset (create: [null, v] for every audited
    property; update: exactly the differing entries, all-equal rows
    suppressed; destroy: [last, null]; empty audited set: '{}'), keys in
    sidecar column-declaration order, values the folds' CAST-AS-VARCHAR
    after-image strings verbatim or null, assembled via
    `build_changes_object_expr`. Reference-valued entries and membership
    member fields translate through `build_identity_translation_sql` per
    `change_edges` (fan-out-free, applied around the lag — order
    irrelevant, both agree); a member field expands in place to its
    `<f>_kind` / `<f>_id` entry pair. `item_id` joins the source's
    `item_surface` translation relation (destroy rows included — never
    the nulled after-image; the owner's identity for a membership
    source), CAST to `log.item_id_type` when non-VARCHAR. `occurred_at`
    renders wallclock through the anchor renderer. Sources UNION ALL in
    declaration order under the total ORDER BY `(event_sim_time,
    item_type, event_class, record_id, membership fields in
    element-schema declaration order, VARCHAR-compared, NULLS FIRST)`.

    Windowed: append rows with `event_sim_time` in [window.start_ns,
    window.end_ns), computed over the full fold — the lag's previous
    after-image may predate the window; membership selects rows, never
    alters content.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        log: The resolved event-log unit.
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """
    arms = [
        _build_membership_arm_sql(sidecar, fork_path, anchor, source, log.item_id_type)
        if source.property is not None
        else _build_records_arm_sql(
            sidecar, fork_path, anchor, source, log.item_id_type
        )
        for source in log.sources
    ]
    union_sql = " UNION ALL ".join(arms)

    where_clause = ""
    if window is not None:
        where_clause = (
            f' WHERE "_log"."event_sim_time" >= {window.start_ns}'
            f' AND "_log"."event_sim_time" < {window.end_ns}'
        )

    return (
        'SELECT "item_type", "item_id", "event", "occurred_at", "changes"'
        f' FROM ({union_sql}) AS "_log"'
        f"{where_clause}"
        ' ORDER BY "_log"."event_sim_time", "_log"."item_type", "_log"."event_class",'
        ' "_log"."_order_record_id", "_log"."_order_fields" NULLS FIRST'
    )
