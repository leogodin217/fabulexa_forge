"""Source export engine: build_source_query_specs, export_source.

The source counterpart of the dimensional exporter's full-export path:
resolves the election (`exporters.election.resolve_election`), builds the
source plan (`exporters.source.plan.build_source_plan`), compiles one render
per output table (`exporters.source.renders`), guards every elected relation
(`exporters.election.check_elected_key_unique`) before any writer runs, and
dispatches to the fmt-selected writer via the shared `write_query_specs`.
Optionally windowed (Unit 2): every spec is tagged its genre's write_mode.
Under `change_delivery: snapshot` (Unit 3), a change-log-genre spec routes to
`build_snapshot_render_sql` instead of the CDC render and is tagged
`write_mode='replace'` when windowed, or `write_mode='create'` for a full
(non-windowed) export — which reconstructs at the tape's end (§ Shaped
state, "One mode semantic, redefined") rather than refusing.

Layer-direction invariant: imports the reader, derivations.guard, the
mode-neutral election module (including its record-index / presentation-key
horizon dispatchers `_record_index_sql` / `_presentation_key_sql` —
recomputed here, not re-derived from the sibling renders module, to guard
the exact relation the render embeds; both are pure functions of their
arguments, so the two computations cannot disagree, per the sprint
contract's recompute-not-thread posture), the sibling source plan/renders
modules, config.models and anchor (TYPE_CHECKING only where runtime use is
not needed), errors, and the mode-neutral query_spec module. Never imports
exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.source.plan import SourceEdgeSurface, SourceTableSpec
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import SourceAnchorRequired
from fabulexa_forge.exporters.base_relations import apply_base_relations
from fabulexa_forge.exporters.election import (
    _presentation_key_sql,
    _record_index_sql,
    build_population_spine_sql,
    check_elected_key_unique,
    resolve_election,
)
from fabulexa_forge.exporters.query_spec import (
    QuerySpec,
    declare_keys_active,
    keys_not_declarable_csv_notice,
    write_query_specs,
)
from fabulexa_forge.exporters.source.plan import (
    _kind_from_records_table,
    build_source_plan,
    resolve_source_table_keys,
)
from fabulexa_forge.exporters.source.renders import (
    build_render_sql,
    build_snapshot_render_sql,
)

#: The two non-record_id surfaces the guard covers, in a fixed order so a
#: mixed edge's guard calls are deterministic across runs.
_GUARD_SURFACES: tuple[Literal["record_index", "presentation_id"], ...] = (
    "record_index",
    "presentation_id",
)

_ANCHOR_REQUIRED_MESSAGE = (
    "source export renders wallclock timestamps and requires a resolved anchor:"
    " the emit declares no runtime block; supply rebase.base_date/timezone or"
    " --base-date/--timezone"
)

#: Windowed write_mode per genre (§ Incremental composition). Full export
#: (window=None) tags every spec 'create' instead, regardless of genre.
_WINDOWED_WRITE_MODE_BY_GENRE: dict[str, Literal["append", "replace"]] = {
    "changelog": "append",
    "reference": "replace",
    "transaction": "append",
    "junction": "append",
}


def _write_mode_for_genre(
    genre: str,
    window: "Window | None",
    change_delivery: Literal["changelog", "snapshot"],
) -> Literal["create", "append", "replace"]:
    """Resolve one table spec's write_mode from its genre, windowing, and delivery.

    Args:
        genre: The resolved output table's genre.
        window: The window to filter to, or None for the full export.
        change_delivery: The source config's delivery mode for change-log
            kinds.

    Returns:
        'create' for a full export; 'replace' for a windowed change-log spec
        under `snapshot` delivery (a full snapshot per window); else the
        genre's windowed write_mode.
    """
    if window is None:
        return "create"
    if genre == "changelog" and change_delivery == "snapshot":
        return "replace"
    return _WINDOWED_WRITE_MODE_BY_GENRE[genre]


def _render_sql_for_spec(
    sidecar: "Sidecar",
    fork_path: str,
    table_spec: "SourceTableSpec",
    anchor: "EffectiveAnchor",
    window: "Window | None",
    change_delivery: Literal["changelog", "snapshot"],
) -> str:
    """Dispatch one output table to its render, honoring snapshot delivery.

    A change-log-genre spec under `snapshot` delivery routes to
    `build_snapshot_render_sql`, which reconstructs at `window.end_ns` when
    windowed or at the tape's end when window is None (a full export); every
    other spec routes to the genre dispatch in `renders.build_render_sql`.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_spec: The resolved output table.
        anchor: The resolved effective anchor.
        window: The window to filter to, or None for the full export.
        change_delivery: The source config's delivery mode for change-log
            kinds.

    Returns:
        The table's complete, ordered SELECT.
    """
    if table_spec.genre == "changelog" and change_delivery == "snapshot":
        return build_snapshot_render_sql(sidecar, fork_path, table_spec, anchor, window)
    return build_render_sql(sidecar, fork_path, table_spec, anchor, window)


def _table_horizon(
    genre: str,
    window: "Window | None",
) -> int | None:
    """Resolve the horizon a table's identity/edge joins compose at.

    Reference/transaction/junction genres carry no value-reconstruction
    horizon of their own (the row is the record's current state) — always
    the tape's end. A change-log genre spec (CDC fold or `snapshot`
    delivery) composes at `window.end_ns` when windowed, else the tape's
    end — the same horizon `renders.py` applies to the render's own joins.

    Args:
        genre: The resolved output table's genre.
        window: The window to filter to, or None for the full export.

    Returns:
        The exclusive horizon, or None for the tape's end.
    """
    if genre == "changelog":
        return window.end_ns if window is not None else None
    return None


def _guard_context_label(base_label: str, window: "Window | None") -> str:
    """Suffix a guard's context label with the window display label, if any.

    Args:
        base_label: The table/column identity (e.g. `"orders.id"`).
        window: The active window, or None for a full/sliced export.

    Returns:
        `base_label`, suffixed `" (<window.label>)"` under an incremental
        invocation.
    """
    return base_label if window is None else f"{base_label} ({window.label})"


def _guard_self_identity(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    table_spec: "SourceTableSpec",
    window: "Window | None",
) -> None:
    """Guard one table's self identity relation, when its election is non-`record_id`.

    Junction genre's `identity_surface` is always `'record_id'` (no own
    identity render), so this is a no-op there. A split unit's relation is
    restricted to its own sub_type via the population spine when the kind
    carries other sub-types (a proper subset); an unsplit unit's relation
    draws from the kind's full domain — no spine.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_spec: The resolved output table.
        window: The active window, or None for a full/sliced export.

    Raises:
        ElectedKeyDuplicate: The elected self identity is not a bijection on
            record_id over its consumed set.
    """
    surface = table_spec.identity_surface
    if surface == "record_id":
        return
    kind = _kind_from_records_table(table_spec.source_table)
    horizon_ns = _table_horizon(table_spec.genre, window)
    relation_sql = (
        _record_index_sql(sidecar, fork_path, kind, horizon_ns)
        if surface == "record_index"
        else _presentation_key_sql(sidecar, fork_path, kind, horizon_ns)
    )
    domain = sidecar.subtype_values(kind)
    spine_sql = (
        build_population_spine_sql(sidecar, fork_path, kind, (table_spec.sub_type,))
        if table_spec.sub_type is not None and len(domain) > 1
        else None
    )
    id_out = dict(table_spec.columns)[surface]
    label = _guard_context_label(f"{table_spec.name}.{id_out}", window)
    check_elected_key_unique(emit, relation_sql, surface, spine_sql, label)


def _guard_edge_surface(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    table_spec: "SourceTableSpec",
    edge: "SourceEdgeSurface",
    window: "Window | None",
) -> None:
    """Guard one referencing column's elected relations, per admitted kind
    and surviving surface group.

    A single-target-kind edge (a reference-annotated `prop__<p>` column, the
    junction owner column) contributes one kind; a junction member field
    contributes every admitted kind (`edge.per_kind_populations`) — each
    guarded independently, per the doc's per-member-kind gate.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_spec: The referencing table's resolved output table.
        edge: The resolved edge.
        window: The active window, or None for a full/sliced export.

    Raises:
        ElectedKeyDuplicate: An admitted surface group's elected relation is
            not a bijection on record_id over its consumed set.
    """
    edge_out = dict(table_spec.columns).get(edge.source_column, edge.source_column)
    horizon_ns = _table_horizon(table_spec.genre, window)
    multi_kind = len(edge.per_kind_populations) > 1

    for target_kind, per_population in edge.per_kind_populations:
        domain = set(sidecar.subtype_values(target_kind))
        base_label = f"{table_spec.name}.{edge_out}"
        if multi_kind:
            base_label = f"{base_label} (member kind '{target_kind}')"
        label = _guard_context_label(base_label, window)

        for surface in _GUARD_SURFACES:
            subset = tuple(
                sub_type
                for sub_type, elected in per_population
                if elected == surface and sub_type is not None
            )
            if not subset:
                continue
            relation_sql = (
                _record_index_sql(sidecar, fork_path, target_kind, horizon_ns)
                if surface == "record_index"
                else _presentation_key_sql(sidecar, fork_path, target_kind, horizon_ns)
            )
            spine_sql = (
                build_population_spine_sql(sidecar, fork_path, target_kind, subset)
                if set(subset) != domain
                else None
            )
            check_elected_key_unique(emit, relation_sql, surface, spine_sql, label)


def build_source_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
    notice_sink: "NoticeSink",
    base_relations: "Mapping[str, str] | None",
) -> list[QuerySpec]:
    """
    Compile the source plan to writer-ready QuerySpecs, optionally windowed.

    The source counterpart of the dimensional compile. Resolves the election
    once (`resolve_election(sidecar, config.keys)`), builds the source plan
    (threading notice_sink and the resolved election), then one SELECT per
    output table composing the reader relations and the row-state-events
    derivation (the mode authors no base-table SQL); every structural
    sim-time column renders wallclock through the shared anchor renderer,
    every change-log payload column casts from the fold's codec VARCHAR back
    to its sidecar type. Immediately after composing each table's render SQL,
    guards every relation it embeds — the self identity relation when the
    unit's own election is non-`record_id` (§ `_guard_self_identity`), and
    each referencing column's admitted surface groups (§ `_guard_edge_surface`)
    — before any writer runs, so a corrupted elected key fails the export
    with nothing written.

    window=None keeps the full-export contract: every spec write_mode='create'.
    With a window, applies per-genre window membership and tags write_mode per
    genre: change-log by event_sim_time (append), transaction by
    last_mutation_sim_time (append), reference as a full replace-class
    snapshot (replace), junction extract-on-change with left_at
    horizon-masked (append). No source genre uses views. Under
    `change_delivery: snapshot`, a change-log-genre spec instead renders the
    state-at derivation: at `window.end_ns` when windowed (write_mode='replace',
    a full snapshot per window), or at the tape's end when window=None (a full
    export, write_mode='create') — "the tape's end" realized structurally, no
    horizon ever computed (§ Shaped state, "One mode semantic, redefined").
    When `config.source.declare_keys` is true, every spec's `keys` is
    resolved via `resolve_source_table_keys` (format-agnostic — resolved
    whatever `fmt`, and identically whether `window` is set or None);
    otherwise every spec's `keys` is None.

    Args:
        emit: The open emit.
        config: The validated export config (mode='source').
        anchor: The resolved effective anchor. Required.
        window: The window to filter to, or None for the full export.
        notice_sink: Receiver for plan notices (slice-only-column-omitted).
        base_relations: Physical base-table name -> replacing relation SELECT.
            See `build_query_specs`' docstring — same contract, threaded
            identically.

    Returns:
        One QuerySpec per output table, in deterministic order.

    Raises:
        SourceAnchorRequired: anchor is None.
        ExportError: The single-branch guard or a source business rule fails
            (the SourceTableSpec resolution errors — § build_source_plan).
        ElectedKeyDuplicate: A corrupted elected key fails the uniqueness
            guard on some composed relation.
        ElectionKindUnknown: A `keys` entry names a kind with no records
            table in the emit.
        ElectionMixedIdentity: An unsplit sub-typed unit's populations elect
            differing identity surfaces.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry.
        ElectionSubTypeUnknown: A `keys` map addresses a sub-type outside the
            kind's discriminator domain, or a flat kind.
        ElectionUnionUnsafe: A uniform presentation_id identity election, or
            a referencing column's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        PresentationKeysInvalidError: `declare_keys` is true, or some
            population elects presentation_id, and the sidecar's
            `presentation_keys` block is present and incoherent.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    if anchor is None:
        raise SourceAnchorRequired(_ANCHOR_REQUIRED_MESSAGE)

    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)
    election = resolve_election(sidecar, config.keys)
    table_specs = build_source_plan(
        sidecar, config.source, notice_sink, election=election
    )

    change_delivery = (
        config.source.change_delivery if config.source is not None else "changelog"
    )
    declare_keys = declare_keys_active(config)

    specs: list[QuerySpec] = []
    for table_spec in table_specs:
        sql = _render_sql_for_spec(
            sidecar, fork_path, table_spec, anchor, window, change_delivery
        )
        _guard_self_identity(emit, sidecar, fork_path, table_spec, window)
        for edge in table_spec.edge_surfaces:
            _guard_edge_surface(emit, sidecar, fork_path, table_spec, edge, window)
        specs.append(
            QuerySpec(
                table_name=table_spec.name,
                sql=apply_base_relations(sql, base_relations),
                write_mode=_write_mode_for_genre(
                    table_spec.genre, window, change_delivery
                ),
                view_name=None,
                view_sql=None,
                keys=(
                    resolve_source_table_keys(sidecar, table_spec, change_delivery)
                    if declare_keys
                    else None
                ),
            )
        )
    return specs


def export_source(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> dict[str, int]:
    """
    Run the source exporter and write the operational dump.

    Builds the full-export source query specs, threading notice_sink to the
    plan, flattens them to name->SQL, and dispatches to the writer selected
    by fmt (mirroring export_dimensional's full-export path). When
    `config.source.declare_keys` is true and `fmt == 'csv'`, emits
    `keys_not_declarable_csv_notice()` to `notice_sink` once, before any data
    is written — CSV carries no constraint surface, so the DuckDB-only
    declaration is dropped for this invocation.

    Args:
        emit: The open emit.
        config: The validated export config (mode='source').
        out: The output target — a directory receiving one <table>.csv per
            output table (fmt='csv'), or the .duckdb file path to create
            (fmt='duckdb').
        fmt: Output format; the CLI constrains the raw string before this
            point.
        anchor: The resolved effective anchor. Source requires one; None
            raises.
        notice_sink: Receiver for plan notices (slice-only-column-omitted,
            keys-not-declarable-csv).

    Returns:
        Mapping of every output table name -> row count written (0-row tables
        are still emitted, never dropped).

    Raises:
        SourceAnchorRequired: anchor is None.
        ExportError: The single-branch guard or a source business rule fails.
        ExportRuntimeError: A writer fails.
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    specs = build_source_query_specs(
        emit, config, anchor, None, notice_sink, base_relations=None
    )
    if declare_keys_active(config) and fmt == "csv":
        notice_sink(keys_not_declarable_csv_notice())
    return write_query_specs(emit, specs, out, fmt)
