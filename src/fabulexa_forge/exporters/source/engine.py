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

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import ExportReport
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations import open_truncated_tape, require_single_branch
from fabulexa_forge.errors import SourceAnchorRequired
from fabulexa_forge.exporters.base_relations import shadow_base_relations
from fabulexa_forge.exporters.companion import (
    validate_overlay_tables,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.election import Election, resolve_election
from fabulexa_forge.exporters.horizon import WindowDelivery, compose_window_delta_sql
from fabulexa_forge.exporters.query_spec import (
    QuerySpec,
    declare_keys_active,
    keys_not_declarable_csv_notice,
    write_query_specs,
)
from fabulexa_forge.exporters.source.events import (
    EVENT_LOG_COLUMNS,
    SourceEventLogPlan,
    build_event_log_sql,
)
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
) -> QuerySpec:
    """Compile one `tables[]` plan unit to its full-export QuerySpec.

    Args:
        sidecar: The plan's sidecar.
        fork_path: The sole branch.
        unit: The resolved state or junction table unit.
        anchor: The resolved wallclock anchor.

    Returns:
        The compiled spec, `write_mode='create'`. `keys` is the unit's
        declared keys for a `state` table (`None` when `declare_keys` is
        off); always `None` for a `junction` table (it declares no keys).
        `provenance`, `author_descriptions`, and `author_table_description`
        are copied verbatim from the plan unit (stamped at plan build);
        `kind_values` stays empty — neither table shape carries a
        kind-name-as-value column; `event_log` stays False — only the
        event-log spec (`build_source_query_specs`) sets it.
    """
    if isinstance(unit, SourceStateTablePlan):
        sql = build_state_render_sql(sidecar, fork_path, unit, anchor)
        keys = unit.keys
    else:
        sql = build_junction_render_sql(sidecar, fork_path, unit, anchor)
        keys = None
    return QuerySpec(
        table_name=unit.name,
        sql=sql,
        write_mode="create",
        keys=keys,
        provenance=unit.provenance,
        author_descriptions=unit.author_descriptions,
        author_table_description=unit.description,
    )


def build_source_query_specs(plan: SourcePlan) -> tuple[QuerySpec, ...]:
    """
    Compile the plan to one full-export QuerySpec per output table.

    Connection-free and pure: every data-dependent guard already ran at
    `build_source_plan`, so this composes SQL only. A windowed export runs
    this same compile per horizon (`build_windowed_source_query_specs`).

    Args:
        plan: The resolved source plan.

    Returns:
        One spec per output table, declared order; the event log last. The
        log's `keys` is its plan unit's — `PRIMARY KEY (id)` under
        `declare_keys`, else None. Every spec's `provenance` and
        `kind_values` are copied verbatim from their plan unit; the log's
        `author_descriptions` stays the QuerySpec default (empty) — the
        events surface declares no `descriptions` field. The log's
        `event_log` is True — the only construction site anywhere that sets
        it; its `author_table_description` stays the QuerySpec default
        (None) — no config surface exists for it.
    """
    specs = [
        _compile_table_spec(plan.sidecar, plan.fork_path, unit, plan.anchor)
        for unit in plan.tables
    ]
    if plan.events is not None:
        log_sql = build_event_log_sql(
            plan.sidecar, plan.fork_path, plan.events, plan.anchor
        )
        specs.append(
            QuerySpec(
                table_name=plan.events.name,
                sql=log_sql,
                write_mode="create",
                keys=plan.events.keys,
                provenance=plan.events.provenance,
                kind_values=plan.events.kind_values,
                event_log=True,
            )
        )
    return tuple(specs)


def source_window_delivery(
    unit: "SourceStateTablePlan | SourceJunctionTablePlan | SourceEventLogPlan",
) -> WindowDelivery:
    """The static delivery class of one source plan unit under a window.

    A `state` table and a `junction` table are `snapshot`: each is the
    end-horizon state whole (a current row per record; one row per
    interval, an interval open at the horizon with a NULL `left_at`),
    replaced every window. The event log is `append`: its rows never
    change and its numbering is tape-anchored, so a window delivers exactly
    the events in its range.

    Args:
        unit: The resolved plan unit.

    Returns:
        'snapshot' for a state or junction table, 'append' for the event log.
    """
    if isinstance(unit, SourceEventLogPlan):
        return "append"
    return "snapshot"


def build_windowed_source_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor",
    election: Election,
    window: "Window",
    notice_sink: "NoticeSink",
    *,
    tables: "Collection[str] | None",
) -> list[QuerySpec]:
    """The source mode's horizon compile, optionally a selection.

    The plan builds whole-config at every horizon opened — the plan is the
    mode's unit of validation, and its data-dependent guards and notices
    are plan-scoped, not per output table. Opens the truncated tape at
    `window.end_ns - 1` always; opens `window.start_ns - 1` (the empty tape
    below the first data instant) only when the event log is among the
    selected units (or `tables` is None and the plan declares one) — the
    tape's sidecar view for column enumeration (`Emit.with_sidecar`), its
    `base_relations` for every base read (`shadow_base_relations`). `tables`
    decides two things only: which units' specs are returned, and whether
    the start horizon opens. A `state` / `junction` unit's spec is its
    end-horizon query with write_mode 'replace'; the event log's is `end
    EXCEPT ALL start` ordered by its columns with write_mode 'append'. Each
    horizon compile is the full plan build, so its plan notices reach the
    sink once per horizon opened and its data-dependent guards run against
    the physical tape through the shared connection.

    Args:
        emit: The open physical emit.
        config: The full export config (mode: source).
        anchor: The resolved wallclock anchor.
        election: The resolved key-election view.
        window: The half-open window to compile.
        notice_sink: Receiver for plan notices, once per horizon opened.
        tables: The output table names to return, set semantics, or None
            for every unit. Every name must be an output table of the
            plan — the seam validates this before calling; an unknown name
            fails an `assert`.

    Returns:
        One QuerySpec per selected unit, declared order, the event log last.

    Raises:
        ExportError: A source business rule fails for a plan at an opened
            horizon.
    """
    fork_path = require_single_branch(emit.sidecar)

    def compile_at(at_sim_time: int) -> tuple[SourcePlan, list[QuerySpec]]:
        tape = open_truncated_tape(emit.sidecar, fork_path, at_sim_time)
        plan = build_source_plan(
            emit.with_sidecar(tape.sidecar), config, anchor, election, notice_sink
        )
        return plan, [
            replace(spec, sql=shadow_base_relations(spec.sql, tape.base_relations))
            for spec in build_source_query_specs(plan)
        ]

    end_plan, end_specs = compile_at(window.end_ns - 1)
    units: list[SourceStateTablePlan | SourceJunctionTablePlan | SourceEventLogPlan] = (
        list(end_plan.tables)
    )
    if end_plan.events is not None:
        units.append(end_plan.events)

    if tables is not None:
        declared = {unit.name for unit in units}
        assert declared.issuperset(tables), (
            f"tables selects undeclared name(s): {sorted(set(tables) - declared)}"
        )

    event_log_selected = end_plan.events is not None and (
        tables is None or end_plan.events.name in tables
    )
    start_specs = compile_at(window.start_ns - 1)[1] if event_log_selected else None

    specs: list[QuerySpec] = []
    for index, unit in enumerate(units):
        if tables is not None and unit.name not in tables:
            continue
        end_spec = end_specs[index]
        if source_window_delivery(unit) == "snapshot":
            specs.append(replace(end_spec, write_mode="replace"))
        else:
            assert start_specs is not None
            specs.append(
                replace(
                    end_spec,
                    sql=compose_window_delta_sql(
                        end_spec.sql, start_specs[index].sql, EVENT_LOG_COLUMNS
                    ),
                    write_mode="append",
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
    overlay: "ReadmeOverlay | None",
) -> "ExportReport":
    """
    Run the source exporter and write the operational dump.

    Resolves the election (`resolve_election(sidecar, config.keys)`), builds
    the full-export source plan (`build_source_plan(..., windowed=False,
    ...)`), compiles it (`build_source_query_specs(plan)`). Immediately
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
    plan = build_source_plan(emit, config, resolved_anchor, election, notice_sink)
    specs = list(build_source_query_specs(plan))
    if overlay is not None:
        validate_overlay_tables(overlay, [spec.table_name for spec in specs])
    if declare_keys_active(config) and fmt == "csv":
        notice_sink(keys_not_declarable_csv_notice())
    report = write_query_specs(emit, specs, out, fmt)
    write_companion_artifacts(emit, config, fmt, anchor, report, overlay, out, None)
    return report
