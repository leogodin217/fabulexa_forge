"""Per-genre render SQL builders for source-mode export.

Composes the reader's faithful-read relations (`build_records_relation_sql`,
`build_membership_relation_sql`) and the row-state-events fold
(`build_row_state_events_sql`) into the four genre renders `build_source_plan`
resolves each output table to: change-log (the fold, VARCHAR-cast back to each
column's sidecar type), reference/transaction (the faithful records relation,
discriminator-filtered for a split unit), and junction (the faithful
membership-interval relation). Every structural sim-time column renders
wallclock through the shared anchor renderer (`render_anchor_timestamp_expr`);
every render carries its genre's total `ORDER BY` over raw sim-time keys and
identity — never a rendered timestamp (§ Ordering and determinism).
`build_snapshot_render_sql` composes the state-at derivation instead, for a
change-log kind delivered under `change_delivery: snapshot`; the engine
dispatches to it, not `build_render_sql`. Windowed, it reconstructs at
`window.end_ns` (`build_state_at_sql`); horizon-less (a full export), it
reconstructs at the tape's end (`build_state_at_end_sql`) — "the tape's end"
realized structurally, no horizon ever computed.

Elected identity (`spec.identity_surface`) and edge (`spec.edge_surfaces`)
columns are rendered via the record-index / presentation-key derivations,
LEFT JOINed onto the genre's own relation, mirroring base's
`_key_join_clauses` pattern: the self-identity join at the table's horizon
(end-of-tape for reference/transaction/junction — these genres carry no
value-reconstruction horizon of their own; `window.end_ns` when windowed,
else end-of-tape, for a change-log kind — CDC fold or snapshot delivery
alike, since the fold/state-at relation carries neither `record_index` nor a
horizon-honest `presentation_id` on `d`/absent rows); one join per
referencing column resolving a non-default surface, keyed on `record_id`
(single target kind — a reference-annotated `prop__<p>` column or the
junction owner column) or on `(member_kind, record_id)` via a `UNION ALL`
across the admitted kind universe (a junction member column — member kind is
per-row). A spec with every surface at its default `record_id` composes
byte-identical SQL — no join, no CASE.

Layer-direction invariant: imports the reader, the derivations layer (the
row-state-events fold, the state-at derivation, the record-index and
presentation-key derivations), fabulexa_forge.anchor, fabulexa_forge._sql,
the sibling source.columns and source.plan modules (the latter's
`_MEMBER_PREFIX` / `_MEMBER_ID_SUFFIX` name constants at runtime, mirroring
base's runtime import of `_self_identity`; `SourceEdgeSurface` /
`SourceTableSpec` TYPE_CHECKING only), config.models (TYPE_CHECKING only),
and stdlib. Never imports exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.source.plan import SourceEdgeSurface, SourceTableSpec
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.presentation_key import (
    build_presentation_key_at_end_sql,
    build_presentation_key_at_sql,
)
from fabulexa_forge.derivations.record_index import (
    build_record_index_at_end_sql,
    build_record_index_at_sql,
)
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.derivations.state_at import (
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX
from fabulexa_forge.exporters.source.plan import _MEMBER_ID_SUFFIX, _MEMBER_PREFIX
from fabulexa_forge.reader.records_columns import structural_instant_columns
from fabulexa_forge.reader.relations import (
    build_membership_relation_sql,
    build_records_relation_sql,
)

#: The records-table structural sim-time columns rendered wallclock in the
#: reference/transaction render (§ Operational presentation defaults) —
#: resolved through the reader's structural-temporal surface.
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

#: The change-log fold's fixed prefix columns, rendered verbatim (op,
#: record_id) or through the anchor renderer (event_sim_time) — never CAST
#: back to a sidecar type the way a payload/presentation_id column is.
_CHANGELOG_VERBATIM_COLUMNS: frozenset[str] = frozenset({"op", "record_id"})

#: The state-at derivation's own columns rendered verbatim in the snapshot
#: render — `record_id` is passthrough, `active` is a native computed boolean
#: (never codec VARCHAR). `created_sim_time` / `deactivated_at` render
#: wallclock (via `_RECORDS_WALLCLOCK_COLUMNS`, a superset); every other
#: column (`presentation_id`, `prop__<p>`) is codec VARCHAR and CASTs back.
_SNAPSHOT_VERBATIM_COLUMNS: frozenset[str] = frozenset({"record_id", "active"})


def _column_types(sidecar: "Sidecar", table_name: str) -> dict[str, str]:
    """Map every column of `table_name` to its declared sidecar DuckDB type.

    Args:
        sidecar: The open emit's sidecar.
        table_name: A sidecar table name.

    Returns:
        {column name -> DuckDB type}, in no particular order.
    """
    return {col.name: col.type for col in sidecar.columns(table_name)}


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
    verbatim, aliased passthrough of the source relation's value — including a
    reference-annotated `prop__` column, which lands id-only and unjoined (the
    genre's definition; the consumer joins).

    Args:
        src: The source column name (on the wrapped relation aliased `alias`).
        out: The resolved output column name.
        alias: The SQL alias of the wrapped faithful-read relation.
        anchor: The resolved effective anchor.
        wallclock_columns: The structural sim-time column names for this genre.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out>"`.
    """
    qualified = f'"{alias}"."{src}"'
    if src in wallclock_columns:
        return render_anchor_timestamp_expr(anchor, qualified, out)
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
        alias: The SQL alias of the wrapped faithful-read relation.
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
    return render_anchor_timestamp_expr(anchor, masked_source, out)


# ---------------------------------------------------------------------------
# Key election: identity + edge joins
# ---------------------------------------------------------------------------

#: The self-identity join's table alias (record_index or presentation_id).
_SELF_IDENTITY_ALIAS = "_self_ident"


def _record_index_sql(
    sidecar: "Sidecar", fork_path: str, kind: str, horizon_ns: int | None
) -> str:
    """Compose the record-index resident for one kind at a render's horizon
    selection — recomputed here (never re-derived from `plan.py`), so the
    engine's guard call recomputes the identical string, per the sprint
    contract's recompute-not-thread posture.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The record kind whose index relation to build.
        horizon_ns: The exclusive horizon, or None for the tape's end.

    Returns:
        A complete SELECT producing `RECORD_INDEX_COLUMNS` for `kind`.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
    """
    return (
        build_record_index_at_sql(sidecar, fork_path, kind, horizon_ns)
        if horizon_ns is not None
        else build_record_index_at_end_sql(sidecar, fork_path, kind)
    )


def _presentation_key_sql(
    sidecar: "Sidecar", fork_path: str, kind: str, horizon_ns: int | None
) -> str:
    """Compose the presentation-key resident for one kind at a render's
    horizon selection — `_record_index_sql`'s exact sibling.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The record kind whose presentation-key relation to build.
        horizon_ns: The exclusive horizon, or None for the tape's end.

    Returns:
        A complete SELECT producing `PRESENTATION_KEY_COLUMNS` for `kind`.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
        ExportError: `records__<kind>` declares no `presentation_id` column —
            a caller gating error.
    """
    return (
        build_presentation_key_at_sql(sidecar, fork_path, kind, horizon_ns)
        if horizon_ns is not None
        else build_presentation_key_at_end_sql(sidecar, fork_path, kind)
    )


def _population_case_expr(
    discriminator_expr: str,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
    exprs: "Mapping[KeySurface, str]",
) -> str:
    """Build the CASE-dispatch value expression choosing one population's
    surface value.

    A flat (single, `sub_type=None`) population needs no CASE — its lone
    surface applies unconditionally (`election`'s per-row population
    resolution reduces to a constant for a single-population set).

    Args:
        discriminator_expr: The qualified `prop__<kind>_type` expression
            (e.g. `'"_rec"."prop__actor_type"'`) to dispatch on; unused for a
            flat population.
        per_population: The admitted kind's gated per-population election.
        exprs: Surface -> the value expression to select for that surface.

    Returns:
        A bare SQL value expression (a CASE, or the single arm unconditionally).
    """
    if len(per_population) == 1 and per_population[0][0] is None:
        return exprs[per_population[0][1]]
    arms = []
    for sub_type, surface in per_population:
        assert sub_type is not None, "a multi-population set is always sub-typed"
        arms.append(
            f"WHEN {discriminator_expr} = {_sql_literal(sub_type)}"
            f" THEN {exprs[surface]}"
        )
    return "CASE " + " ".join(arms) + " END"


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
        kind: The unit's record kind.
        identity_surface: The unit's own resolved identity election.
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
        identity_surface: The unit's own resolved identity election; never
            `record_id` (callers only invoke this under a non-default
            election).

    Returns:
        The qualified value expression on `_SELF_IDENTITY_ALIAS`.
    """
    return f'"{_SELF_IDENTITY_ALIAS}"."{identity_surface}"'


def _mixed_single_kind_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    horizon_ns: int | None,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
) -> str:
    """Compose one `(record_id, rendered_value)` VARCHAR relation for one
    kind's admitted populations — base's `_mixed_edge_relation_sql`
    generalized to a possibly-flat kind (no CASE needed when unsplit).

    Reads the per-row population from the target's own records-spine
    discriminator (never a fold after-image, per the doc's per-row
    resolution rule). A target absent from the target kind's own records
    relation (dangled sentinel) has no row in this relation at all, so the
    consuming LEFT JOIN yields NULL. Joins the record-index / presentation-key
    residents only when `per_population` actually elects that surface
    somewhere — a kind admitted only for its (possibly uniform) `record_id`
    populations (e.g. every kind in a junction member field's universe that
    the field's election never touches) needs neither, and a kind carrying
    no `presentation_id` column at all must not have that relation composed.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The admitted kind.
        horizon_ns: The render's horizon selection.
        per_population: The kind's gated per-population election.

    Returns:
        A complete SELECT producing `(record_id, rendered_value)`, one row
        per record of `kind`, `rendered_value` VARCHAR.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
    """
    surfaces = {surface for _, surface in per_population}
    records_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
    discriminator_expr = f'"_rec"."{_PROP_PREFIX}{kind}_type"'
    exprs: "Mapping[KeySurface, str]" = {
        "record_id": 'CAST("_rec"."record_id" AS VARCHAR)',
        "record_index": 'CAST("_idx"."record_index" AS VARCHAR)',
        "presentation_id": 'CAST("_pid"."presentation_id" AS VARCHAR)',
    }
    value_sql = _population_case_expr(discriminator_expr, per_population, exprs)

    joins = ""
    if "record_index" in surfaces:
        index_sql = _record_index_sql(sidecar, fork_path, kind, horizon_ns)
        joins += (
            f' LEFT JOIN ({index_sql}) AS "_idx"'
            ' ON "_rec"."record_id" = "_idx"."record_id"'
        )
    if "presentation_id" in surfaces:
        presentation_sql = _presentation_key_sql(sidecar, fork_path, kind, horizon_ns)
        joins += (
            f' LEFT JOIN ({presentation_sql}) AS "_pid"'
            ' ON "_rec"."record_id" = "_pid"."record_id"'
        )
    return (
        f'SELECT "_rec"."record_id" AS "record_id", {value_sql} AS "rendered_value"'
        f' FROM ({records_sql}) AS "_rec"{joins}'
    )


def _edge_alias(source_column: str) -> str:
    """The join alias for one referencing column's elected relation.

    Args:
        source_column: The referencing column's source identity.

    Returns:
        A per-column alias, unique among a table's joins.
    """
    return f"_edge__{source_column}"


def _single_kind_edge_join_and_expr(
    sidecar: "Sidecar",
    fork_path: str,
    edge: "SourceEdgeSurface",
    horizon_ns: int | None,
    join_key_expr: str,
) -> tuple[str, str] | None:
    """Resolve one single-target-kind referencing column's join + value expr.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        edge: The resolved edge (`len(edge.target_kinds) == 1`).
        horizon_ns: The render's horizon selection.
        join_key_expr: The qualified reference-column value expression on
            the render's own relation to join against.

    Returns:
        None when every admitted population elects record_id (byte-identical
        — no join, verbatim); else `(join clause, value expression)`.
    """
    kind, per_population = edge.per_kind_populations[0]
    surfaces = {surface for _, surface in per_population}
    alias = _edge_alias(edge.source_column)
    if surfaces == {"record_id"}:
        return None
    if surfaces == {"record_index"}:
        rel_sql = _record_index_sql(sidecar, fork_path, kind, horizon_ns)
        join = (
            f' LEFT JOIN ({rel_sql}) AS "{alias}"'
            f' ON {join_key_expr} = "{alias}"."record_id"'
        )
        return join, f'"{alias}"."record_index"'
    if surfaces == {"presentation_id"}:
        rel_sql = _presentation_key_sql(sidecar, fork_path, kind, horizon_ns)
        join = (
            f' LEFT JOIN ({rel_sql}) AS "{alias}"'
            f' ON {join_key_expr} = "{alias}"."record_id"'
        )
        return join, f'"{alias}"."presentation_id"'
    rel_sql = _mixed_single_kind_relation_sql(
        sidecar, fork_path, kind, horizon_ns, per_population
    )
    join = (
        f' LEFT JOIN ({rel_sql}) AS "{alias}"'
        f' ON {join_key_expr} = "{alias}"."record_id"'
    )
    return join, f'"{alias}"."rendered_value"'


def _member_edge_join_and_expr(
    sidecar: "Sidecar",
    fork_path: str,
    edge: "SourceEdgeSurface",
    horizon_ns: int | None,
    kind_col_expr: str,
    id_col_expr: str,
) -> tuple[str, str] | None:
    """Resolve one junction member field's join + value expr, over the union
    of every admitted kind's relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        edge: The resolved member-field edge (`len(edge.target_kinds) > 1`).
        horizon_ns: The render's horizon selection.
        kind_col_expr: The qualified `member__<f>__kind` expression on the
            render's own relation.
        id_col_expr: The qualified `member__<f>__id` expression on the
            render's own relation.

    Returns:
        None when every admitted population, over every admitted kind,
        elects record_id (byte-identical — no join, verbatim); else `(join
        clause, value expression)`.
    """
    all_surfaces = {
        surface
        for _, per_population in edge.per_kind_populations
        for _, surface in per_population
    }
    if all_surfaces == {"record_id"}:
        return None
    alias = _edge_alias(edge.source_column)
    parts = []
    for kind, per_population in edge.per_kind_populations:
        rel_sql = _mixed_single_kind_relation_sql(
            sidecar, fork_path, kind, horizon_ns, per_population
        )
        parts.append(
            f'SELECT {_sql_literal(kind)} AS "kind", "record_id", "rendered_value"'
            f' FROM ({rel_sql}) AS "_k"'
        )
    union_sql = " UNION ALL ".join(parts)
    join = (
        f' LEFT JOIN ({union_sql}) AS "{alias}"'
        f' ON {kind_col_expr} = "{alias}"."kind"'
        f' AND {id_col_expr} = "{alias}"."record_id"'
    )
    return join, f'"{alias}"."rendered_value"'


def _resolve_edge_render(
    sidecar: "Sidecar",
    fork_path: str,
    edge: "SourceEdgeSurface",
    horizon_ns: int | None,
    join_key_expr: str,
    kind_col_expr: str | None,
) -> tuple[str, str] | None:
    """Dispatch one edge to its single-kind or member-field resolver.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        edge: The resolved edge.
        horizon_ns: The render's horizon selection.
        join_key_expr: The qualified reference/id-column expression on the
            render's own relation.
        kind_col_expr: The qualified `member__<f>__kind` expression, for a
            member-field edge; None for a single-target-kind edge.

    Returns:
        None (byte-identical — no join) or `(join clause, value expression)`.
    """
    if len(edge.target_kinds) == 1:
        return _single_kind_edge_join_and_expr(
            sidecar, fork_path, edge, horizon_ns, join_key_expr
        )
    assert kind_col_expr is not None, "a member-field edge needs the kind column"
    return _member_edge_join_and_expr(
        sidecar, fork_path, edge, horizon_ns, kind_col_expr, join_key_expr
    )


def build_records_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """Build the reference/transaction render: the faithful records relation.

    Both genres share this one render — differing only in the genre label
    (role semantics for the consumer, not a schema difference) and, when
    windowed, in whether the window predicate applies at all. Wraps
    `build_records_relation_sql` (the reader's faithful, unprojected records
    read, discriminator-filtered for a split unit) and projects `spec.columns`,
    rendering the structural sim-time columns wallclock. With a window:
    transaction filters to `last_mutation_sim_time` in `[window.start_ns,
    window.end_ns)` (append); reference carries no predicate — a full
    current-state snapshot every window (replace). Neither genre carries a
    value-reconstruction horizon of its own (the row is the record's current
    state), so `spec.identity_surface` / `spec.edge_surfaces` joins always
    compose at the tape's end (`horizon_ns=None`) — the doc's horizonless
    rule. A spec with every surface at its default `record_id` composes
    byte-identical SQL to today.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved reference/transaction output table.
        anchor: The resolved effective anchor.
        window: The window to filter to (transaction genre only), or None
            for the full export.

    Returns:
        A complete SELECT, ordered by `(created_sim_time, record_id)` (raw,
        never the rendered `created_at`).
    """
    kind = spec.source_table[len("records__") :]
    discriminator_filter = (
        {f"{_PROP_PREFIX}{kind}_type": spec.sub_type}
        if spec.sub_type is not None
        else {}
    )
    relation_sql = build_records_relation_sql(
        sidecar, fork_path, kind, discriminator_filter
    )

    joins = [
        _self_identity_join_clause(
            sidecar, fork_path, kind, spec.identity_surface, None, '"_rec"."record_id"'
        )
    ]
    edges_by_source = {edge.source_column: edge for edge in spec.edge_surfaces}
    edge_exprs: dict[str, str] = {}
    for src, edge in edges_by_source.items():
        resolved = _resolve_edge_render(
            sidecar, fork_path, edge, None, f'"_rec"."{src}"', None
        )
        if resolved is not None:
            join_clause, value_expr = resolved
            joins.append(join_clause)
            edge_exprs[src] = value_expr

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src == spec.identity_surface and spec.identity_surface != "record_id":
            select_parts.append(
                f'{_self_identity_value_expr(spec.identity_surface)} AS "{out}"'
            )
        elif src in edge_exprs:
            select_parts.append(f'{edge_exprs[src]} AS "{out}"')
        else:
            select_parts.append(
                _render_wallclock_column(
                    src, out, "_rec", anchor, _RECORDS_WALLCLOCK_COLUMNS
                )
            )
    select_list = ", ".join(select_parts)
    joins_sql = "".join(joins)

    where_clause = ""
    if window is not None and spec.genre == "transaction":
        where_clause = (
            f" WHERE {_half_open_predicate('_rec', 'last_mutation_sim_time', window)}"
        )
    return (
        f'SELECT {select_list} FROM ({relation_sql}) AS "_rec"{joins_sql}'
        f"{where_clause}"
        ' ORDER BY "_rec"."created_sim_time", "_rec"."record_id"'
    )


def build_junction_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """Build the junction render: the faithful membership-interval relation.

    Wraps `build_membership_relation_sql` (the reader's faithful, unprojected
    membership read) and projects `spec.columns`, rendering `joined_sim_time`
    / `left_sim_time` wallclock (`left_at` stays `NULL` while the membership is
    open — faithful, never fabricated). With a window: extract-on-change — a
    row is emitted per window in which the interval's join or leave (or both)
    lands, with `left_at` horizon-masked to `NULL` unless `left_sim_time <
    window.end_ns` (masking never recomputation). A full export (window=None)
    carries unmasked `left_at`, one row per interval — unchanged.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved junction output table.
        anchor: The resolved effective anchor.
        window: The window to filter to, or None for the full export.

    Returns:
        A complete SELECT, ordered by `(record_id, joined_sim_time,
        element-field columns in element-schema declaration order, compared
        as VARCHAR with NULLS FIRST)` — raw sim-time, never `joined_at`.
    """
    table = sidecar.table(spec.source_table)
    owner_kind = table.record_kind
    property_name = table.property
    assert owner_kind is not None and property_name is not None, (
        "membership table must declare record_kind and property"
    )
    relation_sql = build_membership_relation_sql(
        sidecar, fork_path, owner_kind, property_name, {}
    )

    joins: list[str] = []
    edge_exprs: dict[str, str] = {}
    for edge in spec.edge_surfaces:
        if edge.source_column == "record_id":
            join_key_expr = '"_mem"."record_id"'
            kind_col_expr = None
        else:
            field = edge.source_column[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]
            join_key_expr = f'"_mem"."member__{field}__id"'
            kind_col_expr = f'"_mem"."member__{field}__kind"'
        resolved = _resolve_edge_render(
            sidecar, fork_path, edge, None, join_key_expr, kind_col_expr
        )
        if resolved is not None:
            join_clause, value_expr = resolved
            joins.append(join_clause)
            edge_exprs[edge.source_column] = value_expr
    joins_sql = "".join(joins)

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src in edge_exprs:
            select_parts.append(f'{edge_exprs[src]} AS "{out}"')
        elif src == "left_sim_time" and window is not None:
            select_parts.append(
                _junction_masked_left_at_expr(src, out, "_mem", anchor, window)
            )
        else:
            select_parts.append(
                _render_wallclock_column(
                    src, out, "_mem", anchor, _JUNCTION_WALLCLOCK_COLUMNS
                )
            )
    select_list = ", ".join(select_parts)

    where_clause = ""
    if window is not None:
        where_clause = (
            f" WHERE ({_half_open_predicate('_mem', 'joined_sim_time', window)})"
            ' OR ("_mem"."left_sim_time" IS NOT NULL AND'
            f" {_half_open_predicate('_mem', 'left_sim_time', window)})"
        )

    element_columns = [
        col.name
        for col in sidecar.columns(spec.source_table)
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


def build_changelog_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """Build the change-log render: the row-state-events fold, wallclock-rendered
    and cast back from the fold's codec VARCHAR to each column's sidecar type.

    A change-log kind is never split (§ The sub-type split): the fold is
    invoked once per kind, carrying no discriminator predicate, so a `d` row
    (whose after-image is already `NULL` from the fold) is never misfiled to a
    sub-type. With a window: filters to `event_sim_time` in `[window.start_ns,
    window.end_ns)` (append). The fold's property set derives from
    `spec.columns`' prop__ sources — the pattern the snapshot render already
    uses — so a plan-narrowed (policy-omitted slice_only columns excluded)
    spec folds only the delivered properties; the row set (c/u/d and `seq`)
    is unchanged by the narrowing (column-projection-only invariance). Under a
    non-`record_id` `spec.identity_surface`, a post-fold LEFT JOIN onto the
    record-index / presentation-key derivation supersedes the fold's own
    `record_id`/`presentation_id`-after-image slot — the doc's specific
    called-out case: the after-image is `NULL` on a `d` row, but the join
    lands on the records spine, where the value is populated (identity is
    not an after-image). The join composes at `window.end_ns` when windowed,
    else the tape's end — the doc's horizon-binding rule. `spec.edge_surfaces`
    is always empty for a change-log spec — reference-valued `prop__<p>`
    columns render verbatim regardless of election, per the doc's per-row
    rendering table.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved change-log output table.
        anchor: The resolved effective anchor.
        window: The window to filter to, or None for the full export.

    Returns:
        A complete SELECT, ordered by `(event_sim_time, event_class,
        record_id)` — the fold's own order (raw, never `changed_at`).
    """
    kind = spec.source_table[len("records__") :]
    properties = frozenset(
        src[len(_PROP_PREFIX) :]
        for src, _ in spec.columns
        if src.startswith(_PROP_PREFIX)
    )
    fold_sql = build_row_state_events_sql(sidecar, fork_path, kind, properties)
    col_types = _column_types(sidecar, spec.source_table)
    horizon_ns = window.end_ns if window is not None else None

    join_sql = _self_identity_join_clause(
        sidecar,
        fork_path,
        kind,
        spec.identity_surface,
        horizon_ns,
        '"_fold"."record_id"',
    )

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src == spec.identity_surface and spec.identity_surface != "record_id":
            select_parts.append(
                f'{_self_identity_value_expr(spec.identity_surface)} AS "{out}"'
            )
        elif src == "event_sim_time":
            select_parts.append(
                render_anchor_timestamp_expr(anchor, '"_fold"."event_sim_time"', out)
            )
        elif src in _CHANGELOG_VERBATIM_COLUMNS:
            select_parts.append(f'"_fold"."{src}" AS "{out}"')
        else:
            # presentation_id (non-elected) or a prop__<p> payload column: the
            # fold's after-image is codec VARCHAR; CAST back to the sidecar type.
            select_parts.append(f'CAST("_fold"."{src}" AS {col_types[src]}) AS "{out}"')

    select_list = ", ".join(select_parts)
    where_clause = ""
    if window is not None:
        where_clause = (
            f" WHERE {_half_open_predicate('_fold', 'event_sim_time', window)}"
        )
    return (
        f'SELECT {select_list} FROM ({fold_sql}) AS "_fold"{join_sql}'
        f"{where_clause}"
        ' ORDER BY "_fold"."event_sim_time", "_fold"."event_class",'
        ' "_fold"."record_id"'
    )


def build_snapshot_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """Build the snapshot render: the state-at derivation, windowed or at the
    tape's end.

    Composes `build_state_at_sql` at `horizon = window.end_ns` when windowed,
    or `build_state_at_end_sql` (no horizon) for a full export — for a
    change-log kind delivered under `change_delivery: snapshot`, a full-table
    reconstruction of every record's as-of state (growing window over window
    when windowed; `write_mode` is the engine's concern in either case). Two
    documented deviations from the CDC render: no `updated_at` (there is no
    per-event timestamp to render), and `active` / `deactivated_at` are the
    derivation's own rendered columns rather than values read verbatim off the
    source relation. `spec.identity_surface` / `spec.edge_surfaces` joins
    compose at the same horizon as the state-at reconstruction itself (the
    doc's "same horizon as the table's value reconstruction" rule) — state-at
    carries no `record_index` natively, so a non-`record_id` identity
    election always needs the join here (unlike reference/transaction, whose
    faithful read already carries it).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved change-log output table (snapshot column shape —
            `build_source_plan` under `change_delivery: snapshot`).
        anchor: The resolved effective anchor.
        window: The window whose exclusive end is the reconstruction horizon,
            or None to reconstruct at the tape's end (a full export).

    Returns:
        A complete SELECT, ordered by `(created_sim_time, record_id)` — the
        derivation's own order (raw, never a rendered timestamp).
    """
    kind = spec.source_table[len("records__") :]
    properties = frozenset(
        src[len(_PROP_PREFIX) :]
        for src, _ in spec.columns
        if src.startswith(_PROP_PREFIX)
    )
    horizon_ns = window.end_ns if window is not None else None
    state_at_sql = (
        build_state_at_sql(sidecar, fork_path, kind, properties, horizon_ns)
        if horizon_ns is not None
        else build_state_at_end_sql(sidecar, fork_path, kind, properties)
    )
    col_types = _column_types(sidecar, spec.source_table)

    joins = [
        _self_identity_join_clause(
            sidecar,
            fork_path,
            kind,
            spec.identity_surface,
            horizon_ns,
            '"_snap"."record_id"',
        )
    ]
    edges_by_source = {edge.source_column: edge for edge in spec.edge_surfaces}
    edge_exprs: dict[str, str] = {}
    for src, edge in edges_by_source.items():
        resolved = _resolve_edge_render(
            sidecar, fork_path, edge, horizon_ns, f'"_snap"."{src}"', None
        )
        if resolved is not None:
            join_clause, value_expr = resolved
            joins.append(join_clause)
            edge_exprs[src] = value_expr
    joins_sql = "".join(joins)

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src == spec.identity_surface and spec.identity_surface != "record_id":
            select_parts.append(
                f'{_self_identity_value_expr(spec.identity_surface)} AS "{out}"'
            )
        elif src in edge_exprs:
            select_parts.append(f'{edge_exprs[src]} AS "{out}"')
        elif src in _SNAPSHOT_VERBATIM_COLUMNS:
            select_parts.append(f'"_snap"."{src}" AS "{out}"')
        elif src in _RECORDS_WALLCLOCK_COLUMNS:
            select_parts.append(
                _render_wallclock_column(
                    src, out, "_snap", anchor, _RECORDS_WALLCLOCK_COLUMNS
                )
            )
        else:
            # presentation_id (non-elected) or a prop__<p> payload column: the
            # fold's after-image is codec VARCHAR; CAST back to the sidecar type.
            select_parts.append(f'CAST("_snap"."{src}" AS {col_types[src]}) AS "{out}"')

    select_list = ", ".join(select_parts)
    return (
        f'SELECT {select_list} FROM ({state_at_sql}) AS "_snap"{joins_sql}'
        ' ORDER BY "_snap"."created_sim_time", "_snap"."record_id"'
    )


def build_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> str:
    """Dispatch a resolved output table to its genre's render builder.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved output table.
        anchor: The resolved effective anchor.
        window: The window to filter to, or None for the full export.

    Returns:
        The genre's complete, ordered SELECT.
    """
    if spec.genre == "changelog":
        return build_changelog_render_sql(sidecar, fork_path, spec, anchor, window)
    if spec.genre == "junction":
        return build_junction_render_sql(sidecar, fork_path, spec, anchor, window)
    return build_records_render_sql(sidecar, fork_path, spec, anchor, window)
