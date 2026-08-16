"""Table render SQL builders for source-mode export: `state` and `junction`.

The two declared-table renders `build_source_plan` resolves output tables to
(`docs/sprints/source-declared-tables/spec.md` § 3b): `build_state_render_sql`
(one current row per record of a declared table's populations) and
`build_junction_render_sql` (one row per membership interval). The event log
is its own render, `exporters/source/events.py` (§ 3c) — composes the
row-state-events / membership-events folds plus lag/diff/JSON machinery this
module has no use for. The old genre trichotomy (change-log / reference /
transaction) and its dispatcher `build_render_sql` are gone with the declared
grammar; the engine dispatches on the plan unit's own type instead.

Both renders wrap a faithful reader relation (`build_records_relation_sql`,
`build_membership_relation_sql`) at full export, or a reconstruction
(`build_state_at_sql`, `state` only) when windowed; every structural sim-time
column renders wallclock through the shared anchor renderer
(`render_anchor_temporal_expr`); every render carries its own total
`ORDER BY` over raw sim-time keys and identity — never a rendered timestamp
(§ Ordering and determinism). `build_selection_spine_sql` is the one row-
selection seam (source-row-selection sprint § The parent lookup): a
`record_id`-producing SELECT over a kind's records spine, AND-composing a
population filter with a resolved `where` conjunction — `build_state_render_sql`
composes its own predicate inline (over its own base relation), while
`build_junction_render_sql` semi-joins the membership rows' owner `record_id`
against this seam called on the owner kind.

Elected identity (`table.identity_surface`, `state` only) and edge
(`table.edge_surfaces`) columns are rendered via `build_identity_translation_sql`
(`exporters.election`, § 3a) — the shared per-row identity-translation
relation — LEFT JOINed onto the render's own relation: the self-identity join
at the render's own horizon (`_self_identity_join_clause`, mirroring base's
`_key_join_clauses` pattern — the surface's own value-existence horizon,
distinct from the horizon-free identity-translation relation an edge joins);
one join per referencing/member column resolving a non-default surface,
keyed on `record_id` (a single target kind) or `(member_kind, record_id)`
via a `UNION ALL` across the admitted kind universe (a junction member
column — member kind is per-row). A table with every surface at its default
`record_id` composes byte-identical SQL — no join, no CASE.

Layer-direction invariant: imports the reader, the derivations layer only
via `build_state_at_sql` (windowed `state` reconstruction), the mode-neutral
election module (`build_identity_translation_sql`, and the record-index /
presentation-key horizon dispatchers `_record_index_sql` / `_presentation_key_sql`
the self-identity join composes, shared with base's renders — doc § module
placement), fabulexa_forge.anchor, fabulexa_forge._sql (`render_predicate_condition`
composes a `where` entry's condition), the sibling source.columns
(`_PROP_PREFIX`, and the one labeling authority `build_kind_label_expr` the
junction render's `member__<f>__kind` column renders through) and source.plan
modules (`_column_types` and the latter's `_MEMBER_PREFIX` /
`_MEMBER_ID_SUFFIX` / `_MEMBER_KIND_SUFFIX` / `_RECORDS_TABLE_PREFIX` name
constants at runtime, mirroring base's runtime import of `_self_identity`;
`SourceEdgeSurface` / `SourceStateTablePlan` / `SourceJunctionTablePlan` /
`SourceWhereEntry` TYPE_CHECKING only), `exporters.populations` (`Population`,
TYPE_CHECKING only), config.models (TYPE_CHECKING only), and stdlib. Never
imports exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.populations import Population
    from fabulexa_forge.exporters.source.plan import (
        SourceEdgeSurface,
        SourceJunctionTablePlan,
        SourceStateTablePlan,
        SourceWhereEntry,
    )
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal, render_predicate_condition
from fabulexa_forge.anchor import render_anchor_temporal_expr
from fabulexa_forge.derivations.state_at import build_state_at_sql
from fabulexa_forge.exporters.election import (
    _presentation_key_sql,
    _record_index_sql,
    build_identity_translation_sql,
)
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX, build_kind_label_expr
from fabulexa_forge.exporters.source.plan import (
    _MEMBER_ID_SUFFIX,
    _MEMBER_KIND_SUFFIX,
    _MEMBER_PREFIX,
    _RECORDS_TABLE_PREFIX,
    _column_types,
)
from fabulexa_forge.reader.records_columns import structural_instant_columns
from fabulexa_forge.reader.relations import (
    build_membership_relation_sql,
    build_records_relation_sql,
)

#: The records-table structural sim-time columns rendered wallclock in the
#: `state` render (§ Operational presentation defaults) — resolved through
#: the reader's structural-temporal surface.
_RECORDS_WALLCLOCK_COLUMNS: frozenset[str] = frozenset(
    structural_instant_columns("records")
)

#: The membership-table structural sim-time columns rendered wallclock in the
#: junction render — resolved through the reader's structural-temporal
#: surface.
_JUNCTION_WALLCLOCK_COLUMNS: frozenset[str] = frozenset(
    structural_instant_columns("membership")
)

#: The membership table's fixed, non-element prefix columns — excluded from
#: the junction render's element-field ORDER BY tail.
_JUNCTION_FIXED_COLUMNS: frozenset[str] = frozenset(
    {"fork_path", "record_id", "joined_sim_time", "left_sim_time"}
)

#: The `state-at` derivation's own columns rendered verbatim in a windowed
#: `state` render — `record_id` is passthrough, `active` is a native computed
#: boolean (never codec VARCHAR). `created_sim_time` / `deactivated_at`
#: render wallclock (via `_RECORDS_WALLCLOCK_COLUMNS`, a superset); every
#: other windowed column (`presentation_id`, `prop__<p>`) is codec VARCHAR
#: and CASTs back to its sidecar type.
_STATE_AT_VERBATIM_COLUMNS: frozenset[str] = frozenset({"record_id", "active"})


def _render_wallclock_column(
    src: str,
    out: str,
    alias: str,
    anchor: "EffectiveAnchor",
    wallclock_columns: "frozenset[str]",
) -> str:
    """Render one faithful-read column expression.

    A structural sim-time column (a member of `wallclock_columns`) renders
    wallclock through the shared anchor renderer; every other column is a
    verbatim, aliased passthrough of the source relation's value.

    Args:
        src: The source column name (on the wrapped relation aliased `alias`).
        out: The resolved output column name.
        alias: The SQL alias of the wrapped source relation.
        anchor: The resolved effective anchor.
        wallclock_columns: The structural sim-time column names for this render.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out>"`.
    """
    qualified = f'"{alias}"."{src}"'
    if src in wallclock_columns:
        return render_anchor_temporal_expr(anchor, qualified, out, "timestamp")
    return f'{qualified} AS "{out}"'


def _half_open_predicate(alias: str, column: str, window: "Window") -> str:
    """Build a half-open sim-time range predicate over one aliased raw column.

    Args:
        alias: The SQL alias the render wraps its source relation in.
        column: The raw (unrendered) sim-time column name on that relation.
        window: The window to filter to.

    Returns:
        A bare boolean SQL fragment: `"<alias>"."<column>" >= start AND
        "<alias>"."<column>" < end` (no leading WHERE).
    """
    return (
        f'"{alias}"."{column}" >= {window.start_ns}'
        f' AND "{alias}"."{column}" < {window.end_ns}'
    )


def _junction_masked_left_at_expr(
    src: str,
    out: str,
    alias: str,
    anchor: "EffectiveAnchor",
    window: "Window",
) -> str:
    """Render `left_at` horizon-masked to the window's exclusive end.

    NULL while the leave has not happened yet, or lands at or after the
    window's end_ns (still open as of this window's horizon); otherwise the
    same wallclock rendering the full export uses. The masking wraps the raw
    source expression fed to the shared anchor renderer — never recomputes,
    never fabricates.

    Args:
        src: The source column name (`left_sim_time`).
        out: The resolved output column name.
        alias: The SQL alias of the wrapped source relation.
        anchor: The resolved effective anchor.
        window: The window whose end_ns is the masking horizon.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out>"`.
    """
    qualified = f'"{alias}"."{src}"'
    masked_source = (
        f"CASE WHEN {qualified} IS NULL OR {qualified} >= {window.end_ns}"
        f" THEN NULL ELSE {qualified} END"
    )
    return render_anchor_temporal_expr(anchor, masked_source, out, "timestamp")


# ---------------------------------------------------------------------------
# Key election: identity + edge joins
# ---------------------------------------------------------------------------

#: The self-identity join's table alias (record_index or presentation_id).
_SELF_IDENTITY_ALIAS = "_self_ident"


def _self_identity_join_clause(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    identity_surface: "KeySurface",
    horizon_ns: int | None,
    join_key_expr: str,
) -> str:
    """Build the self-identity LEFT JOIN clause, or '' under `record_id`
    (byte-identical — no join).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The table's record kind.
        identity_surface: The table's own resolved identity election.
        horizon_ns: The render's horizon selection.
        join_key_expr: The qualified `record_id` expression on the render's
            own relation to join against (e.g. `'"_rec"."record_id"'`).

    Returns:
        The LEFT JOIN SQL fragment (leading space), or '' under `record_id`.
    """
    if identity_surface == "record_id":
        return ""
    relation_sql = (
        _record_index_sql(sidecar, fork_path, kind, horizon_ns)
        if identity_surface == "record_index"
        else _presentation_key_sql(sidecar, fork_path, kind, horizon_ns)
    )
    return (
        f' LEFT JOIN ({relation_sql}) AS "{_SELF_IDENTITY_ALIAS}"'
        f' ON {join_key_expr} = "{_SELF_IDENTITY_ALIAS}"."record_id"'
    )


def _self_identity_value_expr(identity_surface: "KeySurface") -> str:
    """The self-identity value expression, reading the join alias.

    Args:
        identity_surface: The table's own resolved identity election; never
            `record_id` (callers only invoke this under a non-default
            election).

    Returns:
        The qualified value expression on `_SELF_IDENTITY_ALIAS`.
    """
    return f'"{_SELF_IDENTITY_ALIAS}"."{identity_surface}"'


def _edge_alias(source_column: str) -> str:
    """The join alias for one referencing/member column's elected relation.

    Args:
        source_column: The referencing/member column's source identity.

    Returns:
        A per-column alias, unique among a table's joins.
    """
    return f"_edge__{source_column}"


def _edge_join_and_expr(
    sidecar: "Sidecar",
    fork_path: str,
    edge: "SourceEdgeSurface",
    id_expr: str,
    kind_expr: str | None,
) -> tuple[str, str] | None:
    """Resolve one edge's LEFT JOIN clause and CAST-to-rendered_type value expr.

    Composes `build_identity_translation_sql` (`exporters.election`, § 3a) —
    one relation per admitted kind, horizon-free (elected surfaces are
    creation-constant). A single-target-kind edge (a reference-annotated
    `prop__<p>` column, the junction owner column) joins one such relation;
    a multi-target-kind edge (a junction member field) unions one relation
    per admitted kind, keyed on `(kind_expr, id_expr)` — the per-row device
    `<f>_kind` disambiguates.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        edge: The resolved edge.
        id_expr: The qualified raw record-id-valued expression on the
            render's own relation to join against.
        kind_expr: The qualified `member__<f>__kind` expression, for a
            multi-target-kind edge; None for a single-target-kind edge.

    Returns:
        None when every admitted population elects record_id (byte-identical
        — no join, the render's own verbatim column already carries the
        value); else `(join clause, value expression)`, the value CAST to
        `edge.rendered_type` when it is not `'VARCHAR'`.
    """
    all_surfaces = {
        surface
        for _, per_population in edge.per_kind_populations
        for _, surface in per_population
    }
    if all_surfaces == {"record_id"}:
        return None

    alias = _edge_alias(edge.source_column)
    if len(edge.target_kinds) == 1:
        kind, per_population = edge.per_kind_populations[0]
        rel_sql = build_identity_translation_sql(
            sidecar, fork_path, kind, per_population
        )
        join = (
            f' LEFT JOIN ({rel_sql}) AS "{alias}" ON {id_expr} = "{alias}"."record_id"'
        )
    else:
        assert kind_expr is not None, "a member-field edge needs the kind column"
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
    value_expr = f'"{alias}"."elected_value"'
    if edge.rendered_type != "VARCHAR":
        value_expr = f"CAST({value_expr} AS {edge.rendered_type})"
    return join, value_expr


# ---------------------------------------------------------------------------
# `state` render
# ---------------------------------------------------------------------------


def _needs_population_filter(
    sidecar: "Sidecar", kind: str, populations: "tuple[Population, ...]"
) -> bool:
    """Whether an addressed population set needs a discriminator filter.

    Shared by the `state` render's own population filter and
    `build_selection_spine_sql`'s owner-narrowing (doc § The parent lookup):
    false for a flat kind (single population, `sub_type=None` — no
    discriminator column exists) or when `populations` addresses the kind's
    full declared domain (the design doc's no-op-filter-not-composed rule).

    Args:
        sidecar: The open emit's sidecar.
        kind: The addressed kind.
        populations: The resolved population set.

    Returns:
        True iff a discriminator IN-predicate must be composed.
    """
    if populations[0].sub_type is None:
        return False
    domain = set(sidecar.subtype_values(kind))
    return {p.sub_type for p in populations} != domain


def build_selection_spine_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    populations: "tuple[Population, ...]",
    where: "tuple[SourceWhereEntry, ...]",
) -> str | None:
    """The per-row selection spine: a `record_id`-producing SELECT over the
    kind's records spine of the records satisfying the population set AND
    the predicate conjunction (each entry via `render_predicate_condition`
    on its `source_column` / `sql_type`), or None when neither restricts
    (`populations` covers the declared domain or the kind is flat, and
    `where` is empty). Fan-out-free (`record_id` is unique on the spine);
    evaluates current spine values (doc § Invariants #1). One seam for both
    directions: records-source narrowing, and the parent lookup when callers
    pass the owner kind of a membership unit.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The subject kind (the owner kind for a membership caller).
        populations: The unit's addressed populations.
        where: The unit's resolved predicate entries; empty = none.

    Returns:
        The spine SELECT for an `IN`-semi-join, or None when no restriction
        applies.
    """
    needs_filter = _needs_population_filter(sidecar, kind, populations)
    if not needs_filter and not where:
        return None

    relation_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
    conditions: list[str] = []
    if needs_filter:
        values = ", ".join(
            _sql_literal(p.sub_type) for p in populations if p.sub_type is not None
        )
        conditions.append(f'"_spine"."{_PROP_PREFIX}{kind}_type" IN ({values})')
    conditions.extend(
        render_predicate_condition(
            entry.source_column, entry.value, entry.sql_type, "_spine"
        )
        for entry in where
    )
    return (
        'SELECT "record_id" FROM ('
        f"{relation_sql}"
        f') AS "_spine" WHERE {" AND ".join(conditions)}'
    )


def build_state_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    table: "SourceStateTablePlan",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """The `state` render: one current row per record of the table's
    declared populations.

    Full export (`window is None`): the faithful records read
    (`build_records_relation_sql`), `updated_at` included. Windowed: the
    state-at reconstruction at the window horizon
    (`build_state_at_sql(..., horizon_ns=window.end_ns)`), no `updated_at`
    — the plan already validated the column set against this shape, so
    this builder never re-checks. Both shapes: discriminator filter to
    `table.populations` (omitted when the set is the kind's full domain — a
    no-op filter is not composed), the plan's (source -> output) projection,
    wallclock rendering of structural instants through the anchor renderer,
    identity column per `table.identity_surface` (self-identity join for a
    non-default surface, mirroring the current elected-identity join
    pattern), reference columns per `table.edge_surfaces` (LEFT JOIN on
    `build_identity_translation_sql` per non-default edge, CAST to the
    edge's rendered_type), NULL stays NULL. When `table.where` is
    non-empty, each entry's `render_predicate_condition` (source column,
    sidecar type, base-relation alias) AND-composes into the population
    filter; windowed, a `where` column's value is the state-at fold's
    current-value codec-VARCHAR after-image (constant columns render
    current at every horizon — the mode's declared temporal-honesty
    exception), and DuckDB's implicit VARCHAR-to-typed comparison renders
    the identical typed predicate the full export's raw column carries, so
    row membership is window-invariant. Total ORDER BY
    `(created_sim_time, record_id)` — raw keys, never rendered timestamps.
    A table with every surface at its default composes join-free SQL.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        table: The resolved state-table unit (from a plan whose
            windowed-ness matches `window` presence — the engine enforces
            the pairing; builders trust it).
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """
    kind = table.kind
    needs_filter = _needs_population_filter(sidecar, kind, table.populations)

    horizon_ns: int | None
    if window is None:
        horizon_ns = None
        base_alias = "_rec"
        relation_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
        col_types: dict[str, str] = {}
    else:
        horizon_ns = window.end_ns
        base_alias = "_snap"
        bare_props = frozenset(
            src[len(_PROP_PREFIX) :]
            for src, _ in table.columns
            if src.startswith(_PROP_PREFIX)
        )
        if needs_filter:
            bare_props = bare_props | {f"{kind}_type"}
        bare_props = bare_props | {
            entry.source_column[len(_PROP_PREFIX) :] for entry in table.where
        }
        relation_sql = build_state_at_sql(
            sidecar, fork_path, kind, bare_props, horizon_ns
        )
        col_types = _column_types(sidecar, f"{_RECORDS_TABLE_PREFIX}{kind}")

    joins = [
        _self_identity_join_clause(
            sidecar,
            fork_path,
            kind,
            table.identity_surface,
            horizon_ns,
            f'"{base_alias}"."record_id"',
        )
    ]
    edge_exprs: dict[str, str] = {}
    for edge in table.edge_surfaces:
        resolved = _edge_join_and_expr(
            sidecar, fork_path, edge, f'"{base_alias}"."{edge.source_column}"', None
        )
        if resolved is not None:
            join_clause, value_expr = resolved
            joins.append(join_clause)
            edge_exprs[edge.source_column] = value_expr
    joins_sql = "".join(joins)

    select_parts: list[str] = []
    for src, out in table.columns:
        if src == table.identity_surface and table.identity_surface != "record_id":
            select_parts.append(
                f'{_self_identity_value_expr(table.identity_surface)} AS "{out}"'
            )
        elif src in edge_exprs:
            select_parts.append(f'{edge_exprs[src]} AS "{out}"')
        elif (
            window is not None
            and src not in _STATE_AT_VERBATIM_COLUMNS
            and src not in _RECORDS_WALLCLOCK_COLUMNS
        ):
            # presentation_id (non-elected) or a prop__<p> payload column: the
            # state-at fold's after-image is codec VARCHAR; CAST back.
            select_parts.append(
                f'CAST("{base_alias}"."{src}" AS {col_types[src]}) AS "{out}"'
            )
        else:
            select_parts.append(
                _render_wallclock_column(
                    src, out, base_alias, anchor, _RECORDS_WALLCLOCK_COLUMNS
                )
            )
    select_list = ", ".join(select_parts)

    conditions: list[str] = []
    if needs_filter:
        values = ", ".join(
            _sql_literal(p.sub_type)
            for p in table.populations
            if p.sub_type is not None
        )
        conditions.append(f'"{base_alias}"."{_PROP_PREFIX}{kind}_type" IN ({values})')
    conditions.extend(
        render_predicate_condition(
            entry.source_column, entry.value, entry.sql_type, base_alias
        )
        for entry in table.where
    )
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    return (
        f'SELECT {select_list} FROM ({relation_sql}) AS "{base_alias}"{joins_sql}'
        f"{where_clause}"
        f' ORDER BY "{base_alias}"."created_sim_time", "{base_alias}"."record_id"'
    )


# ---------------------------------------------------------------------------
# `junction` render
# ---------------------------------------------------------------------------


def build_junction_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    table: "SourceJunctionTablePlan",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """The `junction` render: one row per membership interval.

    Carried over from the current junction render in shape (faithful
    membership relation, wallclock `joined_at` / `left_at` with open
    intervals NULL, member ids in the target population's elected surface
    per row) — now projection-aware: renders exactly `table.columns` (the
    owner column always present; a member pair's two columns project
    independently; per-row election resolution consults the member kind
    internally even when `<f>_kind` is omitted). A projected
    `member__<f>__kind` column's value renders through
    `build_kind_label_expr(table.kind_labels)` — identity fall-through,
    byte-identical passthrough when no labels are declared. When
    `table.owner_populations` restricts or `table.where` is non-empty, the
    membership rows' owner `record_id` is semi-joined against
    `build_selection_spine_sql(table.owner_kind, …)` (doc § The parent
    lookup) — no owner attribute projects, only membership. Windowed:
    extract-on-change over interval activity, `left_at` horizon-masked at
    `window.end_ns`; owner selection is window-invariant (constant-gated), so
    it applies identically at every horizon. Total ORDER BY `(record_id,
    joined_sim_time, element fields in element-schema declaration order,
    VARCHAR-compared, NULLS FIRST)`.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        table: The resolved junction unit.
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The render SELECT.
    """
    relation_sql = build_membership_relation_sql(
        sidecar, fork_path, table.owner_kind, table.property, {}
    )

    joins: list[str] = []
    edge_exprs: dict[str, str] = {}
    for edge in table.edge_surfaces:
        if edge.source_column == "record_id":
            id_expr = '"_mem"."record_id"'
            kind_expr = None
        else:
            field = edge.source_column[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]
            id_expr = f'"_mem"."member__{field}__id"'
            kind_expr = f'"_mem"."member__{field}__kind"'
        resolved = _edge_join_and_expr(sidecar, fork_path, edge, id_expr, kind_expr)
        if resolved is not None:
            join_clause, value_expr = resolved
            joins.append(join_clause)
            edge_exprs[edge.source_column] = value_expr
    joins_sql = "".join(joins)

    select_parts: list[str] = []
    for src, out in table.columns:
        if src in edge_exprs:
            select_parts.append(f'{edge_exprs[src]} AS "{out}"')
        elif src == "left_sim_time" and window is not None:
            select_parts.append(
                _junction_masked_left_at_expr(src, out, "_mem", anchor, window)
            )
        elif src.startswith(_MEMBER_PREFIX) and src.endswith(_MEMBER_KIND_SUFFIX):
            labeled_expr = build_kind_label_expr(f'"_mem"."{src}"', table.kind_labels)
            select_parts.append(f'{labeled_expr} AS "{out}"')
        else:
            select_parts.append(
                _render_wallclock_column(
                    src, out, "_mem", anchor, _JUNCTION_WALLCLOCK_COLUMNS
                )
            )
    select_list = ", ".join(select_parts)

    spine_sql = build_selection_spine_sql(
        sidecar, fork_path, table.owner_kind, table.owner_populations, table.where
    )
    conditions: list[str] = []
    if spine_sql is not None:
        conditions.append(f'"_mem"."record_id" IN ({spine_sql})')
    if window is not None:
        window_condition = (
            f"({_half_open_predicate('_mem', 'joined_sim_time', window)})"
            ' OR ("_mem"."left_sim_time" IS NOT NULL AND'
            f" {_half_open_predicate('_mem', 'left_sim_time', window)})"
        )
        conditions.append(f"({window_condition})" if conditions else window_condition)
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    element_columns = [
        col.name
        for col in sidecar.columns(table.source_table)
        if col.name not in _JUNCTION_FIXED_COLUMNS
    ]
    order_terms = ['"_mem"."record_id"', '"_mem"."joined_sim_time"']
    order_terms.extend(
        f'CAST("_mem"."{name}" AS VARCHAR) NULLS FIRST' for name in element_columns
    )

    return (
        f'SELECT {select_list} FROM ({relation_sql}) AS "_mem"{joins_sql}'
        f"{where_clause}"
        f" ORDER BY {', '.join(order_terms)}"
    )
