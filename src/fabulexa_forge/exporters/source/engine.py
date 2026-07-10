"""Source export engine: build_source_query_specs, export_source.

The source counterpart of the dimensional exporter's full-export path: builds
the source plan (`exporters.source.plan.build_source_plan`), compiles one
render per output table (`exporters.source.renders`), and dispatches to the
fmt-selected writer via the shared `write_query_specs`. Optionally windowed
(Unit 2): every spec is tagged its genre's write_mode. Under
`change_delivery: snapshot` (Unit 3), a change-log-genre spec routes to
`build_snapshot_render_sql` instead of the CDC render and is tagged
`write_mode='replace'`; a full (non-windowed) export under `snapshot` raises
`SourceSnapshotRequiresWindows`.

Layer-direction invariant: imports the reader, derivations.guard, the source
plan/renders modules, config.models and anchor (TYPE_CHECKING only where
runtime use is not needed), errors, and the mode-neutral query_spec module.
Never imports exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.source.plan import SourceTableSpec
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import SourceAnchorRequired, SourceSnapshotRequiresWindows
from fabulexa_forge.exporters.query_spec import QuerySpec, write_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.exporters.source.renders import (
    build_render_sql,
    build_snapshot_render_sql,
)

_ANCHOR_REQUIRED_MESSAGE = (
    "source export renders wallclock timestamps and requires a resolved anchor:"
    " the emit declares no runtime block; supply rebase.base_date/timezone or"
    " --base-date/--timezone"
)

_SNAPSHOT_REQUIRES_WINDOWS_MESSAGE = (
    "change_delivery: snapshot requires an incremental invocation; a full export"
    " snapshot is current state at slice end"
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
    `build_snapshot_render_sql` (window is guaranteed non-None here — the
    caller raises `SourceSnapshotRequiresWindows` first); every other spec
    routes to the genre dispatch in `renders.build_render_sql`.

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
        assert window is not None, "snapshot delivery requires a window (guarded)"
        return build_snapshot_render_sql(sidecar, fork_path, table_spec, anchor, window)
    return build_render_sql(sidecar, fork_path, table_spec, anchor, window)


def build_source_query_specs(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    window: "Window | None",
) -> list[QuerySpec]:
    """
    Compile the source plan to writer-ready QuerySpecs, optionally windowed.

    The source counterpart of the dimensional compile. Builds the source
    plan, then one SELECT per output table composing the reader relations and
    the row-state-events derivation (the mode authors no base-table SQL);
    every structural sim-time column renders wallclock through the shared
    anchor renderer, every change-log payload column casts from the fold's
    codec VARCHAR back to its sidecar type.

    window=None keeps the full-export contract: every spec write_mode='create'.
    With a window, applies per-genre window membership and tags write_mode per
    genre: change-log by event_sim_time (append), transaction by
    last_mutation_sim_time (append), reference as a full replace-class
    snapshot (replace), junction extract-on-change with left_at
    horizon-masked (append). No source genre uses views. Under
    `change_delivery: snapshot`, a change-log-genre spec instead renders the
    state-at derivation at `window.end_ns` and is tagged write_mode='replace'
    (a full snapshot per window); window=None then raises
    `SourceSnapshotRequiresWindows` rather than degrading to current-state.

    Args:
        emit: The open emit.
        config: The validated export config (mode='source').
        anchor: The resolved effective anchor. Required.
        window: The window to filter to, or None for the full export.

    Returns:
        One QuerySpec per output table, in deterministic order.

    Raises:
        SourceAnchorRequired: anchor is None.
        SourceSnapshotRequiresWindows: `config.source.change_delivery ==
            'snapshot'` and window is None.
        ExportError: The single-branch guard or a source business rule fails
            (the SourceTableSpec resolution errors — § build_source_plan).
    """
    if anchor is None:
        raise SourceAnchorRequired(_ANCHOR_REQUIRED_MESSAGE)

    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)
    table_specs = build_source_plan(sidecar, config.source)

    change_delivery = (
        config.source.change_delivery if config.source is not None else "changelog"
    )
    if change_delivery == "snapshot" and window is None:
        raise SourceSnapshotRequiresWindows(_SNAPSHOT_REQUIRES_WINDOWS_MESSAGE)

    return [
        QuerySpec(
            table_name=table_spec.name,
            sql=_render_sql_for_spec(
                sidecar, fork_path, table_spec, anchor, window, change_delivery
            ),
            write_mode=_write_mode_for_genre(table_spec.genre, window, change_delivery),
            view_name=None,
            view_sql=None,
        )
        for table_spec in table_specs
    ]


def export_source(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
) -> dict[str, int]:
    """
    Run the source exporter and write the operational dump.

    Builds the full-export source query specs, flattens them to name->SQL,
    and dispatches to the writer selected by fmt (mirroring
    export_dimensional's full-export path).

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

    Returns:
        Mapping of every output table name -> row count written (0-row tables
        are still emitted, never dropped).

    Raises:
        SourceAnchorRequired: anchor is None.
        SourceSnapshotRequiresWindows: `config.source.change_delivery ==
            'snapshot'` — a full export always calls the compile with
            window=None, which the mode refuses.
        ExportError: The single-branch guard or a source business rule fails.
        ExportRuntimeError: A writer fails.
    """
    specs = build_source_query_specs(emit, config, anchor, None)
    return write_query_specs(emit, specs, out, fmt)
