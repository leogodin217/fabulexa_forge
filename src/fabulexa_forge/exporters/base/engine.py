"""Base export engine: build_base_query_specs, export_base.

Base's counterpart to the source engine: builds the base plan
(`exporters.base.plan.build_base_plan`), compiles one render per surviving
kind (`exporters.base.renders.build_base_render_sql`) at a single horizon, and
dispatches to the fmt-selected writer via the shared `write_query_specs`.
Base never uses the compile-indirection (`base_relations`) wrapping and never
requires a resolved anchor — unlike source, `anchor=None` renders lifecycle
timestamps as raw sim-time ns instead of raising.

Layer-direction invariant: imports the reader (TYPE_CHECKING only), the
derivations layer's single-branch guard, the sibling base plan/renders
modules, config.models and anchor (TYPE_CHECKING only where runtime use is
not needed), and the mode-neutral query_spec module. Never imports
exporters.dimensional.* or exporters.source.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.incremental.windows import Window
    from fabulexa_forge.reader.emit import Emit

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.plan import build_base_plan, resolve_base_table_keys
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.exporters.query_spec import (
    QuerySpec,
    declare_keys_active,
    keys_not_declarable_csv_notice,
    write_query_specs,
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
    both call. Builds the plan once (threading `notice_sink`), then one
    QuerySpec per surviving kind via `build_base_render_sql`. The horizon is
    `window.end_ns` when `window` is set (a per-window snapshot), else
    `config.base.slice_at + 1` when `slice_at` is set, else None (the tape's
    end). Every base spec is view-less; `write_mode` is `'create'` for a full
    or sliced export and `'replace'` for a windowed snapshot — exactly
    source's snapshot delivery. `base_relations` is not a parameter: base
    never uses the compile-indirection wrapping. When `config.base.
    declare_keys` is true, every spec's `keys` is resolved via
    `resolve_base_table_keys` (format-agnostic — resolved whatever `fmt`, and
    identically whether `window` is set or None); otherwise every spec's
    `keys` is None.

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

    Raises:
        ExportError: A base business rule fails (rename resolution or
            collision).
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)
    plan = build_base_plan(sidecar, config.base, notice_sink)

    horizon_ns = _resolve_horizon_ns(config, window)
    write_mode: Literal["create", "replace"] = (
        "replace" if window is not None else "create"
    )
    declare_keys = declare_keys_active(config)

    return [
        QuerySpec(
            table_name=table_spec.table_name,
            sql=build_base_render_sql(
                sidecar, fork_path, table_spec, anchor, horizon_ns
            ),
            write_mode=write_mode,
            view_name=None,
            view_sql=None,
            keys=resolve_base_table_keys(sidecar, table_spec) if declare_keys else None,
        )
        for table_spec in plan.tables
    ]


def export_base(
    emit: "Emit",
    config: "ExportConfig",
    out: "Path",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> dict[str, int]:
    """
    Run the base exporter and write the flat projection.

    Builds the full-export base query specs (window=None, so the horizon is
    `config.base.slice_at + 1` when set, else the tape's end), then dispatches
    to the fmt-selected writer via the shared `write_query_specs` — mirroring
    `export_source`, minus the anchor requirement. When `config.base.
    declare_keys` is true and `fmt == 'csv'`, emits
    `keys_not_declarable_csv_notice()` to `notice_sink` once, before any data
    is written — CSV carries no constraint surface, so the DuckDB-only
    declaration is dropped for this invocation.

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

    Returns:
        Mapping of every output table name -> row count written (0-row
        tables are still emitted, never dropped).

    Raises:
        ExportError: The single-branch guard or a base business rule fails.
        ExportRuntimeError: A writer fails.
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
    specs = build_base_query_specs(emit, config, anchor, None, notice_sink)
    if declare_keys_active(config) and fmt == "csv":
        notice_sink(keys_not_declarable_csv_notice())
    return write_query_specs(emit, specs, out, fmt)
