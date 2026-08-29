"""Incremental export driver: cursor management, drip orchestration.

Orchestrates the --next (drip) and --from/--to (range) export paths.
No IO opens occur here other than via the writer and cursor modules.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import QuerySpec
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.writers.relation import WrittenRelation

from fabulexa_forge import __version__
from fabulexa_forge.errors import (
    ExportRuntimeError,
    IncrementalConfigMissing,
    IncrementalFingerprintMismatch,
    IncrementalRangeTargetExists,
)
from fabulexa_forge.exporters.companion import (
    validate_overlay_tables,
    write_companion_artifacts,
)
from fabulexa_forge.exporters.companion.artifacts import WindowedArtifactState
from fabulexa_forge.exporters.query_spec import (
    ExportReport,
    TableReport,
    declare_keys_active,
    keys_not_declarable_csv_notice,
    query_spec_output_name,
)
from fabulexa_forge.incremental.cursor import (
    _CURRENT_CURSOR_FORMAT_VERSION,
    Cursor,
    read_cursor,
    write_csv_cursor,
)
from fabulexa_forge.incremental.windows import Window, derive_window
from fabulexa_forge.reader.emit import compute_sidecar_sha256


@dataclass(frozen=True)
class IncrementalOutcome:
    """Result of a --next invocation.

    `row_counts` -- author-facing output name -> real written row count --
    mirrors `report`'s None-iff-drained rule; it is never carried by
    `report`'s own `TableReport.row_count`, which stays None on every
    windowed invocation.
    """

    status: Literal["emitted", "drained"]
    window: Window | None  # None when drained
    report: "ExportReport | None"  # None iff drained
    row_counts: "Mapping[str, int] | None"  # None iff drained


@dataclass(frozen=True)
class WindowedExport:
    """One windowed export's outcome: the manifest-bound report, plus
    per-table row counts for CLI stdout.

    `report.tables[*].row_count` stays None on every windowed invocation (the
    manifest contract windowed exports keep); `row_counts` — keyed by each
    table's author-facing output name, matching `report.tables[*].name` — is
    the real count `write_duckdb_window` / `_write_csv_specs` observed, kept
    only for presentation and never written into a companion artifact.
    """

    report: "ExportReport"
    row_counts: "Mapping[str, int]"


def _get_fork_path(emit: "Emit") -> str:
    """Return the sole branch's fork_path from the emit's sidecar.

    Args:
        emit: The open emit (trunk-only; sole branch guaranteed by engine gate).

    Returns:
        The sole branch's fork_path string.
    """
    branches = emit.sidecar.branches()
    return branches[0].fork_path


def _get_slice_at(emit: "Emit") -> int:
    """Return the sole branch's slice_at from the emit's sidecar.

    Args:
        emit: The open emit (trunk-only; sole branch).

    Returns:
        The sole branch's slice_at value.
    """
    branches = emit.sidecar.branches()
    return branches[0].slice_at


def _build_fingerprint(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor | None",
    fmt: Literal["csv", "duckdb"],
) -> str:
    """Compute the drip fingerprint for the given emit, config, anchor, and fmt.

    Args:
        emit: The open emit.
        config: The validated export config.
        anchor: The resolved anchor, or None.
        fmt: Output format.

    Returns:
        64-char lowercase hex digest.
    """
    from fabulexa_forge.incremental.fingerprint import compute_fingerprint

    sidecar_sha256 = compute_sidecar_sha256(emit)
    fork_path = _get_fork_path(emit)

    return compute_fingerprint(
        config=config,
        anchor=anchor,
        sidecar_sha256=sidecar_sha256,
        fork_path=fork_path,
        fmt=fmt,
        package_version=__version__,
    )


def _windowed_artifact_state(
    window: Window, anchor: "EffectiveAnchor | None"
) -> WindowedArtifactState:
    """The windowed facts one committed window's artifact rewrite records.

    `regime` mirrors the window math's own anchor-presence rule (calendar
    cadence requires a resolved anchor, sim-time cadence requires none — see
    `derive_window` / `parse_range`), never re-derived from
    `config.incremental`, which a --from/--to range carries no block for.
    `next_window_index` is `window.index + 1` under --next, None for a range
    (`window.index` is already None — stateless, no cursor exists).

    Args:
        window: The window just committed.
        anchor: The resolved anchor in force for this invocation, or None.

    Returns:
        The `WindowedArtifactState` for this invocation.
    """
    regime: Literal["calendar", "sim_time"] = (
        "calendar" if anchor is not None else "sim_time"
    )
    next_window_index = window.index + 1 if window.index is not None else None
    return WindowedArtifactState(
        regime=regime, label=window.label, next_window_index=next_window_index
    )


def _write_windowed_artifacts(
    emit: "Emit",
    config: "ExportConfig",
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    report: ExportReport,
    overlay: "ReadmeOverlay | None",
    out: Path,
    window: Window,
) -> None:
    """Whole-state rewrite of both companion artifacts for one committed window.

    Called only once the window's data — and, for --next, its cursor advance
    — are durably committed: by `export_window` itself for a --from/--to
    range (no cursor exists to wait on), and by `export_incremental_next`
    after the cursor write (--next, either format).

    Args:
        emit: The open emit.
        config: The validated export config.
        fmt: The resolved output format.
        anchor: The resolved anchor, or None.
        report: The committed window's per-table report.
        overlay: The parsed README overlay, or None.
        out: The output target — directory (csv) or `.duckdb` file (duckdb).
        window: The window just committed.

    Raises:
        ExportRuntimeError: An artifact file cannot be written.
    """
    write_companion_artifacts(
        emit,
        config,
        fmt,
        anchor,
        report,
        overlay,
        out,
        _windowed_artifact_state(window, anchor),
    )


def _build_windowed_report(
    specs: "list[QuerySpec]",
    written: "Mapping[str, WrittenRelation]",
    *,
    include_keys: bool,
) -> WindowedExport:
    """Assemble a windowed invocation's `WindowedExport` from its written relations.

    `written` is keyed by each spec's physical `table_name` (the writers'
    own dict shape); a table's report entry — and its `row_counts` entry —
    are named for its author-facing output name (the SCD-2 view name where
    one exists). The report's `row_count` is always None — a windowed row
    count is never a manifest fact — while `row_counts` carries the writer's
    real `WrittenRelation.row_count` for CLI presentation. `keys` follows
    the CSV/DuckDB constraint-surface split `write_query_specs` uses for
    full exports: DuckDB carries the spec's declared keys, CSV always None.
    `provenance`, `kind_values`, and `author_descriptions` are forwarded from
    each spec verbatim — windowed and full stamping are identical for the
    same table.

    Args:
        specs: The compiled windowed QuerySpecs, in plan iteration order.
        written: Physical table_name -> its written relation.
        include_keys: True for a DuckDB target, False for CSV.

    Returns:
        One `TableReport` and one row-count entry per spec, in plan
        iteration order.
    """
    return WindowedExport(
        report=ExportReport(
            tables=tuple(
                TableReport(
                    name=query_spec_output_name(spec),
                    columns=written[spec.table_name].columns,
                    row_count=None,
                    keys=spec.keys if include_keys else None,
                    provenance=spec.provenance,
                    kind_values=spec.kind_values,
                    author_descriptions=spec.author_descriptions,
                )
                for spec in specs
            )
        ),
        row_counts={
            query_spec_output_name(spec): written[spec.table_name].row_count
            for spec in specs
        },
    )


def export_window(
    emit: "Emit",
    config: "ExportConfig",
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    window: Window,
    fingerprint: str | None,
    notice_sink: "NoticeSink",
    overlay: "ReadmeOverlay | None",
) -> WindowedExport:
    """Run one pure windowed export (the body --next wraps; also --from/--to).

    The compile step dispatches on `config.mode`: `source` resolves the
    election, builds the windowed source plan (`build_source_plan(...,
    windowed=True, ...)`), and compiles it (`build_source_query_specs(plan,
    window)`); `base` calls `build_base_query_specs`; `dimensional` calls
    `build_query_specs`; all three thread notice_sink to their compile — the
    mode-specific compile contributes only the QuerySpecs, the window math,
    cursor, fingerprint, drained detection, and staging below are
    mode-neutral. Immediately after compiling — before any write — validates
    `overlay`'s `table:` slots against the compiled plan's author-facing
    output names when `overlay` is present. Dispatches to the fmt's windowed
    write path. fingerprint is None iff window.index is None (an
    explicit range): the output is then a standalone artifact — a fresh
    .duckdb / a single drop directory at out (a CSV range stages at the
    sibling <out parent>/.tmp_<label> and renames to out), refused if out
    already exists — with no cursor touched and no bookkeeping tables
    written. Under --next, fingerprint is the drip fingerprint the writer
    stores on warehouse creation.

    One invocation compiles exactly once — an explicit --from/--to range is
    a single range-window — so every plan notice reaches notice_sink once,
    with no forwarding or dedup logic. When the mode section in play has
    `declare_keys` on and fmt is 'csv', `keys_not_declarable_csv_notice()`
    is emitted here, once, before any data is written — never in the
    compiles, the dispatch, or the writers.

    A --from/--to range has no cursor to wait on, so its companion artifacts
    are rewritten whole-state here, immediately after the range's data
    commits. A --next window's artifacts are rewritten by
    `export_incremental_next` instead, after its cursor advance commits.

    Args:
        emit: The open emit.
        config: Validated config (incremental block not required).
        out: Output target per fmt.
        fmt: Output format.
        anchor: The resolved anchor, or None.
        window: The half-open window to export.
        fingerprint: The drip fingerprint (--next), or None (explicit
            range — standalone, bookkeeping-free).
        notice_sink: Receiver for plan notices.
        overlay: The parsed README overlay, or None.

    Returns:
        The invocation's `WindowedExport`: an `ExportReport` with one
        `TableReport` per declared table (`row_count` always None), plus a
        `row_counts` mapping of the same tables' real written counts for
        CLI presentation.

    Raises:
        IncrementalRangeTargetExists: window.index is None and out already
            exists.
        SourceAnchorRequired: config.mode == 'source' and anchor is None.
        ReadmeOverlayUnknownTable: `overlay` names a table the compiled plan
            does not produce.
        ExportError / ExportRuntimeError: As export_dimensional /
            export_source today; plus a failed artifact write for a
            --from/--to range.
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
    from fabulexa_forge.writers.duckdb import write_duckdb_window

    is_range = window.index is None

    # Range path: refuse an existing target
    if is_range and out.exists():
        raise IncrementalRangeTargetExists(
            f"--from/--to target already exists: {out}"
            " (range export never appends; use a fresh path)"
        )

    if config.mode == "source":
        from fabulexa_forge.exporters.election import resolve_election
        from fabulexa_forge.exporters.source.engine import (
            build_source_query_specs,
            require_source_anchor,
        )
        from fabulexa_forge.exporters.source.plan import build_source_plan

        resolved_anchor = require_source_anchor(anchor)
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit,
            config,
            resolved_anchor,
            election,
            windowed=True,
            notices=notice_sink,
        )
        specs = list(build_source_query_specs(plan, window))
    elif config.mode == "base":
        from fabulexa_forge.exporters.base.engine import build_base_query_specs

        specs = build_base_query_specs(emit, config, anchor, window, notice_sink)
    else:
        from fabulexa_forge.exporters.dimensional.engine import build_query_specs
        from fabulexa_forge.exporters.election import resolve_election

        assert config.dimensional is not None
        election = resolve_election(emit.sidecar, config.keys)
        specs = build_query_specs(
            emit,
            config.dimensional,
            anchor,
            window,
            notice_sink,
            base_relations=None,
            election=election,
        )

    if overlay is not None:
        validate_overlay_tables(overlay, [query_spec_output_name(s) for s in specs])

    if fmt == "csv" and declare_keys_active(config):
        notice_sink(keys_not_declarable_csv_notice())

    if fmt == "duckdb":
        written_relations = write_duckdb_window(emit, specs, out, window, fingerprint)
        windowed_export = _build_windowed_report(
            specs, written_relations, include_keys=True
        )
        if is_range:
            _write_windowed_artifacts(
                emit, config, fmt, anchor, windowed_export.report, overlay, out, window
            )
        return windowed_export

    # CSV path
    if is_range:
        # Range: stage into sibling .tmp_<label>, rename to out
        staging_dir = out.parent / f".tmp_{window.label}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        try:
            written = _write_csv_specs(emit, specs, staging_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        try:
            staging_dir.rename(out)
        except Exception as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise ExportRuntimeError(
                f"failed to rename staging dir {staging_dir} to {out}: {exc}"
            ) from exc

        windowed_export = _build_windowed_report(specs, written, include_keys=False)
        _write_windowed_artifacts(
            emit, config, fmt, anchor, windowed_export.report, overlay, out, window
        )
        return windowed_export

    # --next CSV: stage into out/.tmp_<label>; the caller commits the cursor
    # and then the artifacts once staging is renamed.
    out.mkdir(parents=True, exist_ok=True)
    staging_dir = out / f".tmp_{window.label}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        written = _write_csv_specs(emit, specs, staging_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    drop_dir = out / window.label
    if drop_dir.exists():
        shutil.rmtree(drop_dir)

    try:
        staging_dir.rename(drop_dir)
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to rename staging dir {staging_dir} to {drop_dir}: {exc}"
        ) from exc

    return _build_windowed_report(specs, written, include_keys=False)


def _write_csv_specs(
    emit: "Emit",
    specs: "list[QuerySpec]",
    target_dir: Path,
) -> dict[str, "WrittenRelation"]:
    """Write all QuerySpecs as CSVs into target_dir.

    SCD-2 __rows specs use the view_name (author name) as the CSV file stem.
    All other specs use the table_name.

    Args:
        emit: The open emit.
        specs: QuerySpecs to write.
        target_dir: Directory to write CSVs into.

    Returns:
        Mapping of each spec's physical table_name -> its written relation.

    Raises:
        ExportRuntimeError: Any CSV write fails.
    """
    from fabulexa_forge.writers.csv import write_csv

    written: dict[str, "WrittenRelation"] = {}
    for spec in specs:
        author_name = query_spec_output_name(spec)
        written[spec.table_name] = write_csv(emit, author_name, spec.sql, target_dir)
    return written


def export_incremental_next(
    emit: "Emit",
    config: "ExportConfig",
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
    overlay: "ReadmeOverlay | None",
) -> IncrementalOutcome:
    """Emit the next window and advance the cursor; or report drained.

    Reads the cursor of record for fmt, verifies the fingerprint, derives
    the next window, and — unless the window's start_ns exceeds the sole
    branch's slice_at (drained: nothing written, cursor untouched, both
    artifact files untouched) — runs the windowed export (threading
    notice_sink and overlay to export_window) and commits window data and
    cursor advance per the fmt's atomicity rule (duckdb: same transaction;
    csv: stage, rename, then write cursor); once the window's data and
    cursor are both durably committed, rewrites both companion artifacts
    whole-state from the window's report. A fresh target starts at window 0.
    An empty window is emitted, never skipped. A leftover .tmp_* staging
    directory is discarded at the next staging. Each drip invocation
    compiles exactly once and re-emits its compile's notices.

    Args:
        emit: The open emit (trunk-only).
        config: Validated config; `incremental` must be present.
        out: Warehouse .duckdb file path (duckdb) or drop parent
            directory (csv).
        fmt: Output format.
        anchor: The resolved anchor, or None.
        notice_sink: Receiver for plan notices.
        overlay: The parsed README overlay, or None.

    Returns:
        IncrementalOutcome — status 'emitted' with the window and the
        window's `ExportReport`, or 'drained' with neither.

    Raises:
        IncrementalConfigMissing: config.incremental is None.
        IncrementalAnchorRequired / IncrementalPeriodRegimeMismatch:
            cadence regime does not match anchor presence.
        IncrementalFingerprintMismatch: stored fingerprint differs from
            the computed one.
        IncrementalCursorInvalid: per read_cursor.
        ReadmeOverlayUnknownTable: `overlay` names a table the compiled plan
            does not produce.
        ExportError: A windowed business rule fails, or any existing rule.
        ExportRuntimeError: A writer failure; the window's transaction or
            staging directory is rolled back / discarded; or a failed
            artifact write (only possible once data and cursor are sound).
        TemporalClassUnavailableError: Non-conformant temporal pair.
    """
    if config.incremental is None:
        raise IncrementalConfigMissing(
            "--next requires an `incremental` block in the export config"
        )

    incremental = config.incremental

    # Compute the window-0 label for cursor classification
    window_zero = derive_window(0, incremental, anchor)
    window_zero_label = window_zero.label

    # Read the cursor of record
    cursor = read_cursor(out, fmt, window_zero_label)

    # Compute the current fingerprint
    fingerprint = _build_fingerprint(emit, config, anchor, fmt)

    if cursor is not None:
        # Verify fingerprint matches what was stored
        if cursor.fingerprint != fingerprint:
            raise IncrementalFingerprintMismatch(
                f"stored fingerprint {cursor.fingerprint!r} does not match"
                f" the computed fingerprint {fingerprint!r};"
                " the config, emit, anchor, or format changed mid-drip"
            )
        next_index = cursor.next_window_index
    else:
        next_index = 0

    # Derive the next window
    window = derive_window(next_index, incremental, anchor)

    # Drained check: start_ns strictly greater than slice_at
    slice_at = _get_slice_at(emit)
    if window.start_ns > slice_at:
        return IncrementalOutcome(
            status="drained",
            window=None,
            report=None,
            row_counts=None,
        )

    # Run the windowed export
    windowed_export = export_window(
        emit, config, out, fmt, anchor, window, fingerprint, notice_sink, overlay
    )

    # For CSV: write the cursor after the atomic rename
    if fmt == "csv":
        next_cursor = Cursor(
            cursor_format_version=_CURRENT_CURSOR_FORMAT_VERSION,
            fingerprint=fingerprint,
            next_window_index=next_index + 1,
        )
        write_csv_cursor(out, next_cursor)

    # Data (and, for csv, the cursor) are now durably committed: rewrite
    # both companion artifacts whole-state from this window's report.
    _write_windowed_artifacts(
        emit, config, fmt, anchor, windowed_export.report, overlay, out, window
    )

    return IncrementalOutcome(
        status="emitted",
        window=window,
        report=windowed_export.report,
        row_counts=windowed_export.row_counts,
    )
