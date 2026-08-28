"""Source export engine: build_source_query_specs, export_source.

Plan and compile are split (§ 1): `export_source` resolves the election
(`exporters.election.resolve_election`) and builds the source plan
(`exporters.source.plan.build_source_plan`) — the one data-dependent step,
plan-time uniqueness guards included — then `build_source_query_specs(plan,
window)` is a pure, connection-free compile: one `QuerySpec` per plan unit
(`exporters.source.renders.build_state_render_sql` /
`build_junction_render_sql`, `exporters.source.events.build_event_log_sql`),
tables in plan order, the event log last. Full export (`window is None`)
tags every spec `write_mode='create'`; windowed tags `state` `replace`,
`junction` / the event log `append` (§ Incremental composition). The engine
carries no `base_relations` parameter — that compile-indirection rewrite is
now the playback seam's own post-compile step
(`playback.shaped._rewrite_specs_base_relations`, § 2).

Layer-direction invariant: imports the reader (TYPE_CHECKING only), the
mode-neutral election module (`resolve_election`), the sibling source
plan/renders/events modules, config.models, anchor, and notices
(TYPE_CHECKING only where runtime use is not needed), errors, and the
mode-neutral query_spec module. Never imports exporters.dimensional.* or
exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.errors import SourceAnchorRequired
from fabulexa_forge.exporters.companion import (
    validate_overlay_tables,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import (
    QuerySpec,
    declare_keys_active,
    keys_not_declarable_csv_notice,
    write_query_specs,
)
from fabulexa_forge.exporters.source.events import build_event_log_sql
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourcePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import (
    build_junction_render_sql,
    build_state_render_sql,
)

_ANCHOR_REQUIRED_MESSAGE = (
    "source export renders wallclock timestamps and requires a resolved anchor:"
    " the emit declares no runtime block; supply rebase.base_date/timezone or"
    " --base-date/--timezone"
)


def require_source_anchor(anchor: "EffectiveAnchor | None") -> "EffectiveAnchor":
    """Refuse a None anchor resolution for a source invocation.

    `build_source_plan` requires a resolved `EffectiveAnchor` (source has no
    base-mode raw-ns fallback); every caller of it — `export_source`, the
    incremental driver's source branch — checks here first, so the refusal
    is worded identically everywhere it can occur.

    Args:
        anchor: The caller's resolved effective anchor, or None.

    Returns:
        `anchor`, narrowed to non-None.

    Raises:
        SourceAnchorRequired: `anchor` is None.
    """
    if anchor is None:
        raise SourceAnchorRequired(_ANCHOR_REQUIRED_MESSAGE)
    return anchor


def _compile_table_spec(
    sidecar: "Sidecar",
    fork_path: str,
    unit: "SourceStateTablePlan | SourceJunctionTablePlan",
    anchor: "EffectiveAnchor",
    window: "Window | None",
) -> QuerySpec:
    """Compile one `tables[]` plan unit to its QuerySpec.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        unit: The resolved state or junction table unit.
        anchor: The resolved wallclock anchor.
        window: The incremental window, or None for a full export.

    Returns:
        The compiled spec: `write_mode='create'` for a full export;
        windowed, `'replace'` for a `state` unit (a full horizon snapshot
        per window) or `'append'` for a `junction` unit (extract-on-change).
        `keys` is the unit's declared keys for a `state` table (`None` when
        `declare_keys` is off); always `None` for a `junction` table (it
        declares no keys). `provenance` is copied verbatim from the plan
        unit (stamped at plan build); `kind_values` stays empty — neither
        table shape carries a kind-name-as-value column.
    """
    if isinstance(unit, SourceStateTablePlan):
        sql = build_state_render_sql(sidecar, fork_path, unit, anchor, window)
        write_mode: Literal["create", "append", "replace"] = (
            "create" if window is None else "replace"
        )
        keys = unit.keys
    else:
        sql = build_junction_render_sql(sidecar, fork_path, unit, anchor, window)
        write_mode = "create" if window is None else "append"
        keys = None
    return QuerySpec(
        table_name=unit.name,
        sql=sql,
        write_mode=write_mode,
        view_name=None,
        view_sql=None,
        keys=keys,
        provenance=unit.provenance,
    )


def build_source_query_specs(
    plan: SourcePlan,
    window: "Window | None",
) -> tuple[QuerySpec, ...]:
    """
    Compile the plan to one QuerySpec per output table.

    Full-export compile when `window` is None; the windowed compile applies
    the per-render window membership (state: horizon snapshot without
    updated_at; event log: append by event_sim_time; junction:
    extract-on-change with left_at horizon-masking). Connection-free and
    pure: every data-dependent guard already ran at `build_source_plan` (§
    1), so this composes SQL only.

    Args:
        plan: The resolved source plan (built with the matching
            windowed-ness: a non-None `window` pairs with a
            `windowed=True` plan, None with `windowed=False`).
        window: The incremental window, or None for a full export.

    Returns:
        One spec per output table, declared order; the event log last. The
        log's `keys` is its plan unit's — `PRIMARY KEY (id)` under
        `declare_keys`, else None. Every spec's `provenance` and
        `kind_values` are copied verbatim from their plan unit.

    Raises:
        ValueError: `window` presence disagrees with the plan's
            windowed-ness — a caller programming error, never a config
            validation outcome. Otherwise nothing: the plan already
            carries every validated fact, the windowed-shape checks
            included.
    """
    if (window is not None) != plan.windowed:
        raise ValueError(
            f"window presence ({window is not None}) disagrees with the"
            f" plan's windowed-ness ({plan.windowed})"
        )

    specs = [
        _compile_table_spec(plan.sidecar, plan.fork_path, unit, plan.anchor, window)
        for unit in plan.tables
    ]
    if plan.events is not None:
        log_sql = build_event_log_sql(
            plan.sidecar, plan.fork_path, plan.events, plan.anchor, window
        )
        specs.append(
            QuerySpec(
                table_name=plan.events.name,
                sql=log_sql,
                write_mode="create" if window is None else "append",
                view_name=None,
                view_sql=None,
                keys=plan.events.keys,
                provenance=plan.events.provenance,
                kind_values=plan.events.kind_values,
            )
        )
    return tuple(specs)


def export_source(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    overlay: "ReadmeOverlay | None",
) -> "ExportReport":
    """
    Run the source exporter and write the operational dump.

    Resolves the election (`resolve_election(sidecar, config.keys)`), builds
    the full-export source plan (`build_source_plan(..., windowed=False,
    ...)`), compiles it (`build_source_query_specs(plan, None)`). Immediately
    after compiling — before any write — validates `overlay`'s `table:`
    slots against the compiled plan's output tables when `overlay` is
    present. Dispatches to the writer selected by fmt (mirroring
    export_dimensional's full-export path). When `config.source.declare_keys`
    is true and `fmt == 'csv'`, emits `keys_not_declarable_csv_notice()` to
    notice_sink once, before any data is written — CSV carries no constraint
    surface, so the DuckDB-only declaration is dropped for this invocation.
    Writes the companion README + manifest after data delivery and returns
    the report.

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
        overlay: The parsed README overlay, or None.

    Returns:
        The invocation's `ExportReport`: one `TableReport` per output table
        (0-row tables are still emitted, never dropped).

    Raises:
        SourceAnchorRequired: anchor is None.
        ExportError: The single-branch guard or a source business rule fails
            (§ build_source_plan).
        ReadmeOverlayUnknownTable: `overlay` names a table the compiled plan
            does not produce.
        ExportRuntimeError: A writer fails, or the companion artifacts fail
            to write.
        ElectedKeyDuplicate: A corrupted elected key fails the plan-time
            uniqueness guard.
        ElectionKindUnknown, ElectionMixedIdentity,
            ElectionPresentationUndeclared, ElectionSubTypeUnknown,
            ElectionUnionUnsafe: The election resolution or its gates fail.
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    resolved_anchor = require_source_anchor(anchor)
    sidecar = emit.sidecar
    election = resolve_election(sidecar, config.keys)
    plan = build_source_plan(
        emit, config, resolved_anchor, election, windowed=False, notices=notice_sink
    )
    specs = list(build_source_query_specs(plan, None))
    if overlay is not None:
        validate_overlay_tables(overlay, [spec.table_name for spec in specs])
    if declare_keys_active(config) and fmt == "csv":
        notice_sink(keys_not_declarable_csv_notice())
    report = write_query_specs(emit, specs, out, fmt)
    write_companion_artifacts(emit, config, fmt, anchor, report, overlay, out, None)
    return report
