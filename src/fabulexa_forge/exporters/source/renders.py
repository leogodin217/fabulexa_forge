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
dispatches to it, not `build_render_sql`.

Layer-direction invariant: imports the reader, the derivations layer (the
row-state-events fold and the state-at derivation), fabulexa_forge.anchor,
fabulexa_forge.errors, the sibling source.columns module, config.models / the
source plan type (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.exporters.source.plan import SourceTableSpec
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.derivations.state_at import build_state_at_sql
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX
from fabulexa_forge.reader.relations import (
    build_membership_relation_sql,
    build_records_relation_sql,
)

#: The records-table structural sim-time columns rendered wallclock in the
#: reference/transaction render (§ Operational presentation defaults).
_RECORDS_WALLCLOCK_COLUMNS: frozenset[str] = frozenset(
    {"created_sim_time", "deactivated_at", "last_mutation_sim_time"}
)

#: The membership-table structural sim-time columns rendered wallclock in the
#: junction render.
_JUNCTION_WALLCLOCK_COLUMNS: frozenset[str] = frozenset(
    {"joined_sim_time", "left_sim_time"}
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
    current-state snapshot every window (replace).

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

    select_list = ", ".join(
        _render_wallclock_column(src, out, "_rec", anchor, _RECORDS_WALLCLOCK_COLUMNS)
        for src, out in spec.columns
    )
    where_clause = ""
    if window is not None and spec.genre == "transaction":
        where_clause = (
            f" WHERE {_half_open_predicate('_rec', 'last_mutation_sim_time', window)}"
        )
    return (
        f'SELECT {select_list} FROM ({relation_sql}) AS "_rec"'
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

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src == "left_sim_time" and window is not None:
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
        f'SELECT {select_list} FROM ({relation_sql}) AS "_mem"'
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
    is unchanged by the narrowing (column-projection-only invariance).

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

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src == "event_sim_time":
            select_parts.append(
                render_anchor_timestamp_expr(anchor, '"_fold"."event_sim_time"', out)
            )
        elif src in _CHANGELOG_VERBATIM_COLUMNS:
            select_parts.append(f'"_fold"."{src}" AS "{out}"')
        else:
            # presentation_id or a prop__<p> payload column: the fold's
            # after-image is codec VARCHAR; CAST back to the sidecar type.
            select_parts.append(f'CAST("_fold"."{src}" AS {col_types[src]}) AS "{out}"')

    select_list = ", ".join(select_parts)
    where_clause = ""
    if window is not None:
        where_clause = (
            f" WHERE {_half_open_predicate('_fold', 'event_sim_time', window)}"
        )
    return (
        f'SELECT {select_list} FROM ({fold_sql}) AS "_fold"'
        f"{where_clause}"
        ' ORDER BY "_fold"."event_sim_time", "_fold"."event_class",'
        ' "_fold"."record_id"'
    )


def build_snapshot_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window",
) -> str:
    """Build the snapshot render: the state-at derivation at `window.end_ns`.

    Composes `build_state_at_sql` at `horizon = window.end_ns` for a change-log
    kind delivered under `change_delivery: snapshot` — a full-table
    reconstruction of every record's as-of state at this window's exclusive
    end, growing window over window (`write_mode='replace'`; the engine's
    concern). Two documented deviations from the CDC render: no `updated_at`
    (there is no per-event timestamp to render), and `active` /
    `deactivated_at` are the fold's own horizon-rendered columns rather than
    values read verbatim off the source relation.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved change-log output table (snapshot column shape —
            `build_source_plan` under `change_delivery: snapshot`).
        anchor: The resolved effective anchor.
        window: The window whose exclusive end is the reconstruction horizon.

    Returns:
        A complete SELECT, ordered by `(created_sim_time, record_id)` — the
        fold's own order (raw, never a rendered timestamp).
    """
    kind = spec.source_table[len("records__") :]
    properties = frozenset(
        src[len(_PROP_PREFIX) :]
        for src, _ in spec.columns
        if src.startswith(_PROP_PREFIX)
    )
    horizon_ns = window.end_ns
    state_at_sql = build_state_at_sql(sidecar, fork_path, kind, properties, horizon_ns)
    col_types = _column_types(sidecar, spec.source_table)

    select_parts: list[str] = []
    for src, out in spec.columns:
        if src in _SNAPSHOT_VERBATIM_COLUMNS:
            select_parts.append(f'"_snap"."{src}" AS "{out}"')
        elif src in _RECORDS_WALLCLOCK_COLUMNS:
            select_parts.append(
                _render_wallclock_column(
                    src, out, "_snap", anchor, _RECORDS_WALLCLOCK_COLUMNS
                )
            )
        else:
            # presentation_id or a prop__<p> payload column: the fold's
            # after-image is codec VARCHAR; CAST back to the sidecar type.
            select_parts.append(f'CAST("_snap"."{src}" AS {col_types[src]}) AS "{out}"')

    select_list = ", ".join(select_parts)
    return (
        f'SELECT {select_list} FROM ({state_at_sql}) AS "_snap"'
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
