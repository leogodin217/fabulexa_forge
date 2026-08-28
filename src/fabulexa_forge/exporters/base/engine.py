"""Base export engine: build_base_query_specs, export_base.

Base's counterpart to the source engine: resolves the election
(`exporters.election.resolve_election`), builds the base plan
(`exporters.base.plan.build_base_plan`), compiles one render per surviving
kind (`exporters.base.renders.build_base_render_sql`) at a single horizon,
guards every elected relation (`exporters.election.check_elected_key_unique`)
before any writer runs, and dispatches to the fmt-selected writer via the
shared `write_query_specs`. Base never uses the compile-indirection
(`base_relations`) wrapping and never requires a resolved anchor — unlike
source, `anchor=None` renders lifecycle timestamps as raw sim-time ns instead
of raising.

Layer-direction invariant: imports the reader (TYPE_CHECKING only), the
derivations layer's single-branch guard, the mode-neutral election module
(including its record-index / presentation-key horizon dispatchers
`_record_index_sql` / `_presentation_key_sql` — recomputed here, not
re-derived from the sibling renders module, to guard the exact relation the
render embeds; both are pure functions of their arguments, so the two
computations cannot disagree, per the sprint contract's recompute-not-thread
posture), the sibling base plan/renders modules, config.models and anchor
(TYPE_CHECKING only where runtime use is not needed), and the mode-neutral
query_spec module. Never imports exporters.dimensional.* or
exporters.source.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.base.plan import BaseTableSpec, ReferenceKey
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.plan import build_base_plan, resolve_base_table_keys
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.companion import (
    validate_overlay_tables,
    write_companion_artifacts,
)
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

#: The two non-record_id surfaces the guard covers, in a fixed order so a
#: mixed edge's guard calls are deterministic across runs.
_GUARD_SURFACES: tuple[Literal["record_index", "presentation_id"], ...] = (
    "record_index",
    "presentation_id",
)


def _resolve_horizon_ns(config: "ExportConfig", window: "Window | None") -> int | None:
    """Resolve the single reconstruction horizon a base compile renders at.

    Args:
        config: The validated export config (mode='base').
        window: The window to snapshot at, or None for a full or sliced
            export.

    Returns:
        `window.end_ns` when windowed; else `config.base.slice_at + 1` when
        `slice_at` is set; else None (the tape's end).
    """
    if window is not None:
        return window.end_ns
    if config.base is not None and config.base.slice_at is not None:
        return config.base.slice_at + 1
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
    table_spec: "BaseTableSpec",
    horizon_ns: int | None,
    window: "Window | None",
) -> None:
    """Guard one table's self identity relation, when its election is non-`record_id`.

    Base never splits, so the self relation draws from the kind's full
    domain — no population spine.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_spec: The resolved per-kind flat-output shape.
        horizon_ns: The render's horizon selection.
        window: The active window, or None for a full/sliced export.

    Raises:
        ElectedKeyDuplicate: The elected self identity is not a bijection on
            record_id over the kind's full domain.
    """
    surface = table_spec.identity_surface
    if surface == "record_id":
        return
    relation_sql = (
        _record_index_sql(sidecar, fork_path, table_spec.kind, horizon_ns)
        if surface == "record_index"
        else _presentation_key_sql(sidecar, fork_path, table_spec.kind, horizon_ns)
    )
    id_out = table_spec.column_renames[surface]
    label = _guard_context_label(f"{table_spec.table_name}.{id_out}", window)
    check_elected_key_unique(emit, relation_sql, surface, None, label)


def _guard_reference_key(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    table_spec: "BaseTableSpec",
    rk: "ReferenceKey",
    horizon_ns: int | None,
    window: "Window | None",
) -> None:
    """Guard one reference edge's elected relations, per admitted surface group.

    Groups `rk.per_population` by resolved surface (record_id excluded — the
    doc scopes the guard to non-record_id elections); a dropped edge
    (`value_column_shipped=False`, uniform record_index) needs no guard call
    at all — nothing elected renders. Each surviving surface group's relation
    is restricted to that group's sub_types via the population spine when the
    group is a proper subset of the target kind's declared domain.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        table_spec: The referencing table's resolved flat-output shape.
        rk: The resolved reference key.
        horizon_ns: The render's horizon selection.
        window: The active window, or None for a full/sliced export.

    Raises:
        ElectedKeyDuplicate: An admitted surface group's elected relation is
            not a bijection on record_id over its consumed set.
    """
    if not rk.value_column_shipped:
        return
    domain = set(sidecar.subtype_values(rk.target_kind))
    edge_identity = f"prop__{rk.property_name}"
    edge_out = table_spec.column_renames.get(edge_identity, edge_identity)
    label = _guard_context_label(f"{table_spec.table_name}.{edge_out}", window)

    for surface in _GUARD_SURFACES:
        subset = tuple(
            sub_type
            for sub_type, elected in rk.per_population
            if elected == surface and sub_type is not None
        )
        if not subset:
            continue
        relation_sql = (
            _record_index_sql(sidecar, fork_path, rk.target_kind, horizon_ns)
            if surface == "record_index"
            else _presentation_key_sql(sidecar, fork_path, rk.target_kind, horizon_ns)
        )
        spine_sql = (
            build_population_spine_sql(sidecar, fork_path, rk.target_kind, subset)
            if set(subset) != domain
            else None
        )
        check_elected_key_unique(emit, relation_sql, surface, spine_sql, label)


def build_base_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
    notice_sink: "NoticeSink",
) -> list[QuerySpec]:
    """Compile the base plan to writer-ready QuerySpecs at one horizon.

    Base's counterpart to `build_source_query_specs`, and the entry point the
    incremental driver's `mode == 'base'` branch and the full-export CLI path
    both call. Resolves the election once (`resolve_election(sidecar,
    config.keys)`), builds the plan once (threading `notice_sink` and the
    resolved election), then one QuerySpec per surviving kind via
    `build_base_render_sql`. Immediately after composing each table's render
    SQL, guards every relation it embeds — the self identity relation when
    the kind's election is non-`record_id` (§ `_guard_self_identity`), and
    each reference edge's admitted surface groups (§ `_guard_reference_key`)
    — before any writer runs, so a corrupted elected key fails the export
    with nothing written. The horizon is `window.end_ns` when `window` is set
    (a per-window snapshot), else `config.base.slice_at + 1` when `slice_at`
    is set, else None (the tape's end). Every base spec is view-less;
    `write_mode` is `'create'` for a full or sliced export and `'replace'`
    for a windowed snapshot — exactly source's snapshot delivery.
    `base_relations` is not a parameter: base never uses the
    compile-indirection wrapping. When `config.base.declare_keys` is true,
    every spec's `keys` is resolved via `resolve_base_table_keys`
    (format-agnostic — resolved whatever `fmt`, and identically whether
    `window` is set or None); otherwise every spec's `keys` is None.

    Args:
        emit: The open emit.
        config: The validated export config (`mode='base'`).
        anchor: The resolved effective anchor, or None to emit raw sim-time
            ns. Not required — unlike source, base falls back to raw ns.
        window: The window to snapshot at, or None for a full or sliced
            export.
        notice_sink: Receiver for `slice-only-column-omitted` notices.

    Returns:
        One QuerySpec per surviving kind, in deterministic sidecar order.
        Every spec's `provenance` is copied verbatim from its `BaseTableSpec`
        (stamped at plan build); `kind_values` stays empty — base carries no
        kind-name-as-value column.

    Raises:
        ElectedKeyDuplicate: A corrupted elected key fails the uniqueness
            guard on some composed relation.
        ElectionKindUnknown: A `keys` entry names a kind with no records
            table in the emit.
        ElectionMixedIdentity: A sub-typed kind's surviving populations elect
            differing identity surfaces.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry.
        ElectionSubTypeUnknown: A `keys` map addresses a sub-type outside the
            kind's discriminator domain, or a flat kind.
        ElectionUnionUnsafe: A uniform presentation_id identity election, or
            a reference edge's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        ExportError: A base business rule fails (rename resolution or
            collision).
        DateParseSourceColumn: A `date_parse` key does not resolve to a
            declared VARCHAR column.
        PresentationKeysInvalidError: `declare_keys` is true, or some
            population elects presentation_id, and the sidecar's
            `presentation_keys` block is present and incoherent.
        RenderKeyIsInstantColumn: A `render` key does not name an
            instant-carrying structural column of the records category.
        TableNotFoundError: A declared `records__<kind>` table is absent.
        TemporalRenderRequiresAnchor: A `render` entry elects a rendering and
            no anchor resolved.
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)
    election = resolve_election(sidecar, config.keys)
    plan = build_base_plan(
        sidecar, config.base, notice_sink, election=election, anchor=anchor
    )

    horizon_ns = _resolve_horizon_ns(config, window)
    write_mode: Literal["create", "replace"] = (
        "replace" if window is not None else "create"
    )
    declare_keys = declare_keys_active(config)

    specs: list[QuerySpec] = []
    for table_spec in plan.tables:
        sql = build_base_render_sql(sidecar, fork_path, table_spec, anchor, horizon_ns)
        _guard_self_identity(emit, sidecar, fork_path, table_spec, horizon_ns, window)
        for rk in table_spec.reference_keys:
            _guard_reference_key(
                emit, sidecar, fork_path, table_spec, rk, horizon_ns, window
            )
        specs.append(
            QuerySpec(
                table_name=table_spec.table_name,
                sql=sql,
                write_mode=write_mode,
                view_name=None,
                view_sql=None,
                keys=(
                    resolve_base_table_keys(sidecar, table_spec)
                    if declare_keys
                    else None
                ),
                provenance=table_spec.provenance,
            )
        )
    return specs


def export_base(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    overlay: "ReadmeOverlay | None",
) -> "ExportReport":
    """
    Run the base exporter and write the flat projection.

    Builds the full-export base query specs (window=None, so the horizon is
    `config.base.slice_at + 1` when set, else the tape's end). Immediately
    after compiling — before any write — validates `overlay`'s `table:`
    slots against the compiled plan's output tables when `overlay` is
    present. Dispatches to the fmt-selected writer via the shared
    `write_query_specs` — mirroring `export_source`, minus the anchor
    requirement. When `config.base.declare_keys` is true and `fmt == 'csv'`,
    emits `keys_not_declarable_csv_notice()` to `notice_sink` once, before
    any data is written — CSV carries no constraint surface, so the
    DuckDB-only declaration is dropped for this invocation. Writes the
    companion README + manifest after data delivery and returns the report.

    Args:
        emit: The open emit.
        config: The validated export config (mode='base').
        out: Output target — a directory receiving one <table>.csv per output
            table (fmt='csv'), or the .duckdb file path to create
            (fmt='duckdb').
        fmt: Output format; the CLI constrains the raw string before this
            point.
        anchor: The resolved effective anchor, or None. Base does NOT require
            one — None renders lifecycle timestamps as raw sim-time ns.
        notice_sink: Receiver for plan notices (slice-only-column-omitted,
            keys-not-declarable-csv).
        overlay: The parsed README overlay, or None.

    Returns:
        The invocation's `ExportReport`: one `TableReport` per output table
        (0-row tables are still emitted, never dropped).

    Raises:
        ExportError: The single-branch guard or a base business rule fails.
        ReadmeOverlayUnknownTable: `overlay` names a table the compiled plan
            does not produce.
        ExportRuntimeError: A writer fails, or the companion artifacts fail
            to write.
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
    specs = build_base_query_specs(emit, config, anchor, None, notice_sink)
    if overlay is not None:
        validate_overlay_tables(overlay, [spec.table_name for spec in specs])
    if declare_keys_active(config) and fmt == "csv":
        notice_sink(keys_not_declarable_csv_notice())
    report = write_query_specs(emit, specs, out, fmt)
    write_companion_artifacts(emit, config, fmt, anchor, report, overlay, out, None)
    return report
