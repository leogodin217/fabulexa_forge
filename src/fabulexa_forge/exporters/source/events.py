"""Event-log render: the polymorphic audit table (`exporters/source/events.py`).

The one render that composes two folds (`build_row_state_events_sql`,
`build_membership_events_sql`) plus lag/diff/JSON machinery no other render
shares — warrants its own module (`docs/sprints/source-declared-tables/spec.md`
§ 3c). `SourceEventSourcePlan` / `SourceEventLogPlan` are produced by
`plan.py` and compiled by `engine.py`.

**`change_edges.source_column` convention**: a records source's audited
reference-valued property uses `prop__<p>` (matching
`exporters.source.plan`'s `_resolve_reference_prop_edges`); a membership
source's audited member reference field uses `member__<f>__id` (matching
`_resolve_member_field_edge`) — the same source-column identities the
plan-builder derives by calling those same functions.

Layer-direction invariant: imports the reader (through the derivations
layer), the derivations layer's two event folds, the mode-neutral election
module (`build_identity_translation_sql`), `fabulexa_forge.anchor`,
`fabulexa_forge._sql`, the sibling `source.plan` module (`SourceEdgeSurface`
/ `SourceWhereEntry`, TYPE_CHECKING only), the sibling `source.columns`
module (`build_kind_label_expr` — the one labeling authority, also the
junction render's call site), `exporters.populations` (`Population`,
TYPE_CHECKING only), config.models (`KeySurface`, TYPE_CHECKING only), and
stdlib. Never imports `exporters.dimensional.*` or `exporters.streaming.*`.
One exception to "imports at module top": `_narrow_fold_by_spine_sql` imports
the sibling `source.renders` module's `build_selection_spine_sql` locally, at
call time — `renders.py` imports runtime constants from `plan.py`, which
imports this module at runtime to construct `SourceEventSourcePlan` /
`SourceEventLogPlan`, so a module-level import here would cycle back through
`plan.py` mid-initialization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.populations import Population
    from fabulexa_forge.exporters.query_spec import TableKeys
    from fabulexa_forge.exporters.source.plan import SourceEdgeSurface, SourceWhereEntry
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.membership_events import build_membership_events_sql
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.exporters.election import build_identity_translation_sql
from fabulexa_forge.exporters.source.columns import build_kind_label_expr

_PROP_PREFIX = "prop__"


@dataclass(frozen=True)
class SourceEventSourcePlan:
    """One resolved audited population set of the event log.

    Lives in `exporters/source/events.py` (Phase 2 hand-constructs it in
    tests; a later phase's plan builder produces it).
    """

    item_type: str
    """The RESOLVED item-type: the declaration's `item_type` override,
    else the kind's label (owner-half-labeled `<label(K)>.<property>` for
    a membership source), else the contract identity verbatim (the kind
    name for a records source, '<K>.<property>' for a membership source).
    The dereference key, the union-safety gate key, and the order-key
    component."""
    kind: str
    """The audited kind (records source) or the owner kind `<K>`
    (membership source)."""
    property: "str | None"
    """The membership property, or None for a records source."""
    populations: "tuple[Population, ...]"
    """Records source: the addressed atoms (drives the fold's per-row
    discriminator narrowing and the overlap check). Membership source: the
    owner kind's addressed population set — the full declared domain
    absent `sub_types`, else the narrowed subset (doc § The parent lookup);
    drives per-row item_id resolution and, together with a records source's
    own addressed atoms, the selection-aware overlap check (both-declared
    disjoint owner `sub_types` sets show up here as disjoint population
    sets)."""
    audited_properties: "tuple[tuple[str, str], ...]"
    """The audited set as (source bare name, changes output key) pairs,
    sidecar column-declaration order — every tracked- and constant-class
    property (discriminator included, slice_only policy-omitted) narrowed
    by only / widened-by-subtraction via ignore, for a records source; the
    selected element-schema field names for a membership source. Output
    key equals the bare name absent a rename entry. For a membership
    reference field the pair expands at render to `<key>_kind` /
    `<key>_id`. Folds keep receiving the bare names, never output keys
    (`build_row_state_events_sql` / `build_membership_events_sql`)."""
    kind_labels: "tuple[tuple[str, str], ...]"
    """The resolved (kind, label) map threaded to the render for
    `<f>_kind` entry values; identity fall-through for any value not
    listed. Empty when no labels are declared."""
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
    where: "tuple[SourceWhereEntry, ...]" = ()
    """The source's resolved record predicate (doc § The constant-column
    gate; the parent lookup for a membership source), `where` declaration
    order; empty when `where` is absent — config absence is already
    detected at the decl. Narrows the fold's records/intervals
    (`_build_records_arm_sql` / `_build_membership_arm_sql`) and feeds the
    plan-time selection-aware overlap gate's typed-value comparison."""


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
    keys: "TableKeys | None"
    """The log's declared keys: `PRIMARY KEY (id)` under `declare_keys`,
    None when it is off. A constant of the mode — `id` is true by
    construction, so there is nothing to resolve from the emit."""


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


def _narrow_fold_by_spine_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    populations: "tuple[Population, ...]",
    where: "tuple[SourceWhereEntry, ...]",
    fold_sql: str,
) -> str:
    """Narrow one arm's own fold rows to `build_selection_spine_sql`'s
    selection (doc § Row selection), via a `record_id` semi-join — the
    records arm's own population + `where`, or the membership arm's owner
    population + `where` (the parent lookup). Unchanged when the spine
    applies no restriction. Shared by both arms.

    Local import of `build_selection_spine_sql`: `renders.py` imports
    runtime constants from this package's `plan.py`, which itself imports
    this module at runtime to construct `SourceEventSourcePlan` /
    `SourceEventLogPlan` — a module-level import here would cycle back
    through `plan.py` mid-initialization.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The subject kind (the owner kind for the membership arm).
        populations: The arm's addressed populations.
        where: The arm's resolved predicate entries.
        fold_sql: The arm's own fold SELECT, `record_id`-bearing.

    Returns:
        `fold_sql` semi-joined to the spine, or `fold_sql` unchanged.
    """
    from fabulexa_forge.exporters.source.renders import build_selection_spine_sql

    spine_sql = build_selection_spine_sql(sidecar, fork_path, kind, populations, where)
    if spine_sql is None:
        return fold_sql
    return f'SELECT * FROM ({fold_sql}) AS "_f" WHERE "_f"."record_id" IN ({spine_sql})'


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

    Composes `build_row_state_events_sql`, narrowed per row to the
    selection spine (`source.populations` AND `source.where` — doc § Row
    selection) via `_narrow_fold_by_spine_sql`; recodes op c/u/d ->
    create/update/destroy. Old values are a per-record LAG over
    the fold's own audited after-images (translated first where a change
    edge applies), ordered by `(event_sim_time, event_class)` — the fold's
    own canonical order, and total within a record because the fold emits at
    most one event per (record_id, event_sim_time, event_class). Ordering on
    `event_sim_time` alone would be non-deterministic: a record whose update
    and deactivation land on the same sim_time has two events at that
    instant, and swapping them silently corrupts both before-images.
    `changes` is the full object (`build_changes_object_expr`)
    for create/destroy rows, the differing-only object
    (`_build_diff_changes_expr`) for update rows — an update row touching no
    audited property is dropped, keyed by each pair's resolved `changes`
    output key. `item_id` joins the source's `item_surface` translation
    relation on the fold's own `record_id` (never the nulled after-image),
    so it is never NULL.

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
    properties = frozenset(bare for bare, _output in source.audited_properties)
    fold_sql = build_row_state_events_sql(
        sidecar, fork_path, kind, properties, change_scope=properties
    )

    narrowed_sql = _narrow_fold_by_spine_sql(
        sidecar, fork_path, kind, source.populations, source.where, fold_sql
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
    for bare, _output in source.audited_properties:
        raw_expr = f'"_narrowed"."{_PROP_PREFIX}{bare}"'
        edge = edges_by_property.get(bare)
        if edge is None:
            value_selects.append(f'{raw_expr} AS "_val__{bare}"')
            continue
        alias = f"_edge__{bare}"
        join, value_expr = _edge_translation_join(
            sidecar, fork_path, edge, alias, raw_expr, None
        )
        joins.append(join)
        value_selects.append(f'{value_expr} AS "_val__{bare}"')
    joins_sql = "".join(joins)

    valued_select = ", ".join(
        ['"_narrowed".*', f'{item_id_expr} AS "_item_id"', *value_selects]
    )
    valued_sql = (
        f'SELECT {valued_select} FROM ({narrowed_sql}) AS "_narrowed"{joins_sql}'
    )

    lag_selects = [
        (
            f'LAG("_valued"."_val__{bare}") OVER (PARTITION BY "_valued"."record_id"'
            f' ORDER BY "_valued"."event_sim_time", "_valued"."event_class")'
            f' AS "_old__{bare}"'
        )
        for bare, _output in source.audited_properties
    ]
    lagged_select = ", ".join(['"_valued".*', *lag_selects])
    lagged_sql = f'SELECT {lagged_select} FROM ({valued_sql}) AS "_valued"'

    entries = tuple(
        (output, f'"_lagged"."_old__{bare}"', f'"_lagged"."_val__{bare}"')
        for bare, output in source.audited_properties
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

    Composes `build_membership_events_sql`, narrowed per row to the owner
    selection spine (`source.populations` AND `source.where`, the parent
    lookup — doc § Row selection) via `_narrow_fold_by_spine_sql`: every
    `join` / `leave` of an excluded owner's collection is excluded; recodes
    op join/leave -> create/destroy. Every selected field's value lives on
    its own row (a membership row carries no history of its own), so
    old/new derive from the row's own op, not a lag: join -> `[null,
    value]`, leave -> `[value, null]`. A reference field's pair renders
    `<key>_kind` / `<key>_id`, its resolved `changes` output key; the
    `_kind` half's old and new both render through `build_kind_label_expr`
    (identity fall-through, applied once to the underlying value before the
    old/new CASE split, which commutes with labeling). `item_id` joins the
    owner's `item_surface` translation relation on the fold's own
    (already-owner-VARCHAR) `record_id`.

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
    bare_fields = tuple(bare for bare, _output in source.audited_properties)
    fold_sql = build_membership_events_sql(
        sidecar, fork_path, owner_kind, property_name, bare_fields
    )
    fold_sql = _narrow_fold_by_spine_sql(
        sidecar, fork_path, owner_kind, source.populations, source.where, fold_sql
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
    for field, output in source.audited_properties:
        cols = _membership_field_columns(sidecar, table_name, field)
        if len(cols) == 1:
            raw_expr = f'"_fold"."{cols[0]}"'
            entries.append((output, *_join_leave_old_new(raw_expr)))
            continue

        kind_col, id_col = cols
        kind_expr = build_kind_label_expr(f'"_fold"."{kind_col}"', source.kind_labels)
        id_raw_expr = f'"_fold"."{id_col}"'
        edge = edges_by_source_column.get(id_col)
        if edge is None:
            id_value_expr = id_raw_expr
        else:
            alias = f"_edge__{field}"
            join, id_value_expr = _edge_translation_join(
                sidecar, fork_path, edge, alias, id_raw_expr, f'"_fold"."{kind_col}"'
            )
            joins.append(join)
        entries.append((f"{output}_kind", *_join_leave_old_new(kind_expr)))
        entries.append((f"{output}_id", *_join_leave_old_new(id_value_expr)))

    joins_sql = "".join(joins)
    changes_expr = build_changes_object_expr(tuple(entries))
    event_expr = (
        "CASE \"_fold\".\"op\" WHEN 'join' THEN 'create'"
        " WHEN 'leave' THEN 'destroy' END"
    )
    occurred_at_expr = render_anchor_timestamp_expr(
        anchor, '"_fold"."event_sim_time"', "occurred_at"
    )
    order_fields_expr = _membership_sort_key_expr(sidecar, table_name, bare_fields)

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
    fork_path, kind, frozenset(bare names of audited_properties))`,
    narrowed per row to the source's populations through the records-spine
    discriminator; recodes op c/u/d -> create/update/destroy. Per
    membership source: composes `build_membership_events_sql(sidecar,
    fork_path, owner_kind, property, bare field names)` (join -> create,
    leave -> destroy). Old values are a per-record lag over the fold's own
    audited after-images; `changes` is the design-doc JSON changeset
    (create: [null, v] for every audited property; update: exactly the
    differing entries, all-equal rows suppressed; destroy: [last, null];
    empty audited set: '{}'), keyed by each `audited_properties` pair's
    resolved output key (sidecar column-declaration order — rename
    relabels, never reorders), values the folds' CAST-AS-VARCHAR
    after-image strings verbatim or null, assembled via
    `build_changes_object_expr`. Reference-valued entries and membership
    member fields translate through `build_identity_translation_sql` per
    `change_edges` (fan-out-free, applied around the lag — order
    irrelevant, both agree); a member reference field's pair expands in
    place to its `<key>_kind` / `<key>_id` entry pair, the `_kind` half's
    old and new values each rendered through `build_kind_label_expr`
    (identity fall-through). `item_id` joins the source's `item_surface`
    translation relation (destroy rows included — never the nulled
    after-image; the owner's identity for a membership source), CAST to
    `log.item_id_type` when non-VARCHAR. `occurred_at` renders wallclock
    through the anchor renderer. Sources UNION ALL in declaration order
    under the total ORDER BY `(event_sim_time, item_type, event_class,
    record_id, membership fields in element-schema declaration order,
    VARCHAR-compared, NULLS FIRST)` — `item_type` the plan's resolved
    value.

    `id` is the event's 1-based position in that order over every row the
    log emits for the whole tape, across every source — a ROW_NUMBER,
    never a value-based rank: rows tying the order key take consecutive
    numbers. The outermost ORDER BY is `ORDER BY id`, not a restatement of
    the key: the two agree wherever the key is injective, and where it is
    not (a contract-permitted duplicate membership interval, a corrupter's
    duplicated records row) only the former keeps the emitted row order
    monotone in `id`.

    Windowed: append rows with `event_sim_time` in [window.start_ns,
    window.end_ns), computed over the full fold — the lag's previous
    after-image may predate the window; membership selects rows, never
    alters content. `id` is assigned at its own query level *beneath* the
    window predicate, so an event's number is invariant across
    invocations; SQL evaluates WHERE before window functions, so a
    ROW_NUMBER beside the predicate in one SELECT would number only the
    surviving rows. It sits above the arms' update suppression, so a
    suppressed update consumes no number and `id` stays dense.

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

    order_key = (
        '"_log"."event_sim_time", "_log"."item_type", "_log"."event_class",'
        ' "_log"."_order_record_id", "_log"."_order_fields" NULLS FIRST'
    )
    numbered_sql = (
        f'SELECT ROW_NUMBER() OVER (ORDER BY {order_key}) AS "id",'
        ' "_log"."item_type", "_log"."item_id", "_log"."event",'
        ' "_log"."occurred_at", "_log"."changes", "_log"."event_sim_time"'
        f' FROM ({union_sql}) AS "_log"'
    )

    where_clause = ""
    if window is not None:
        where_clause = (
            f' WHERE "_numbered"."event_sim_time" >= {window.start_ns}'
            f' AND "_numbered"."event_sim_time" < {window.end_ns}'
        )

    return (
        'SELECT "id", "item_type", "item_id", "event", "occurred_at", "changes"'
        f' FROM ({numbered_sql}) AS "_numbered"'
        f"{where_clause}"
        ' ORDER BY "id"'
    )
