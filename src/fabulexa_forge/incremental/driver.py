"""Incremental export driver: cursor management, drip orchestration.

Orchestrates the --next (drip) and --from/--to (range) export paths.
No IO opens occur here other than via the writer and cursor modules.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.query_spec import QuerySpec
    from fabulexa_forge.reader.emit import Emit

from fabulexa_forge import __version__
from fabulexa_forge.errors import (
    ExportRuntimeError,
    IncrementalConfigMissing,
    IncrementalFingerprintMismatch,
    IncrementalRangeTargetExists,
)
from fabulexa_forge.exporters.query_spec import (
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


@dataclass(frozen=True)
class IncrementalOutcome:
    """Result of a --next invocation."""

    status: Literal["emitted", "drained"]
    window: Window | None  # None when drained
    row_counts: dict[str, int]  # empty when drained


def _compute_sidecar_sha256(emit: "Emit") -> str:
    """Compute the SHA-256 hex digest of the emit's base.json bytes.

    Args:
        emit: The open emit.

    Returns:
        64-char lowercase hex digest.
    """
    base_json_path = emit.emit_dir / "base.json"
    data = base_json_path.read_bytes()
    return hashlib.sha256(data).hexdigest()


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

    sidecar_sha256 = _compute_sidecar_sha256(emit)
    fork_path = _get_fork_path(emit)

    return compute_fingerprint(
        config=config,
        anchor=anchor,
        sidecar_sha256=sidecar_sha256,
        fork_path=fork_path,
        fmt=fmt,
        package_version=__version__,
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
) -> dict[str, int]:
    """Run one pure windowed export (the body --next wraps; also --from/--to).

    The compile step dispatches on `config.mode`: `source` calls
    `build_source_query_specs`; `base` calls `build_base_query_specs`;
    `dimensional` calls `build_query_specs`; all three thread notice_sink to
    their compile — the mode-specific compile contributes only the
    QuerySpecs, the window math, cursor, fingerprint, drained detection, and
    staging below are mode-neutral. Dispatches to the fmt's windowed write
    path. fingerprint is None iff window.index is None (an
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

    Returns:
        Mapping of every declared table name -> rows written this window
        (snapshot dims report their full snapshot count).

    Raises:
        IncrementalRangeTargetExists: window.index is None and out already
            exists.
        SourceAnchorRequired: config.mode == 'source' and anchor is None.
        ExportError / ExportRuntimeError: As export_dimensional /
            export_source today.
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
        from fabulexa_forge.exporters.source.engine import build_source_query_specs

        specs = build_source_query_specs(
            emit, config, anchor, window, notice_sink, base_relations=None
        )
    elif config.mode == "base":
        from fabulexa_forge.exporters.base.engine import build_base_query_specs

        specs = build_base_query_specs(emit, config, anchor, window, notice_sink)
    else:
        from fabulexa_forge.exporters.dimensional.engine import build_query_specs

        assert config.dimensional is not None
        specs = build_query_specs(
            emit, config.dimensional, anchor, window, notice_sink, base_relations=None
        )

    if fmt == "csv" and declare_keys_active(config):
        notice_sink(keys_not_declarable_csv_notice())

    if fmt == "duckdb":
        return write_duckdb_window(emit, specs, out, window, fingerprint)

    # CSV path
    if is_range:
        # Range: stage into sibling .tmp_<label>, rename to out
        staging_dir = out.parent / f".tmp_{window.label}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        row_counts: dict[str, int] = {}
        try:
            row_counts = _write_csv_specs(emit, specs, staging_dir)
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

        return row_counts

    # --next CSV: stage into out/.tmp_<label>, return counts (cursor written by caller)
    out.mkdir(parents=True, exist_ok=True)
    staging_dir = out / f".tmp_{window.label}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    row_counts = {}
    try:
        row_counts = _write_csv_specs(emit, specs, staging_dir)
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

    return row_counts


def _write_csv_specs(
    emit: "Emit",
    specs: "list[QuerySpec]",
    target_dir: Path,
) -> dict[str, int]:
    """Write all QuerySpecs as CSVs into target_dir.

    SCD-2 __rows specs use the view_name (author name) as the CSV file stem.
    All other specs use the table_name.

    Args:
        emit: The open emit.
        specs: QuerySpecs to write.
        target_dir: Directory to write CSVs into.

    Returns:
        Mapping of author-name -> rows written.

    Raises:
        ExportRuntimeError: Any CSV write fails.
    """
    from fabulexa_forge.writers.csv import write_csv

    row_counts: dict[str, int] = {}
    for spec in specs:
        author_name = query_spec_output_name(spec)
        rows = write_csv(emit, author_name, spec.sql, target_dir)
        row_counts[author_name] = rows
    return row_counts


def export_incremental_next(
    emit: "Emit",
    config: "ExportConfig",
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> IncrementalOutcome:
    """Emit the next window and advance the cursor; or report drained.

    Reads the cursor of record for fmt, verifies the fingerprint, derives
    the next window, and — unless the window's start_ns exceeds the sole
    branch's slice_at (drained: nothing written, cursor untouched) — runs
    the windowed export (threading notice_sink to export_window) and commits
    window data and cursor advance per the fmt's atomicity rule (duckdb:
    same transaction; csv: stage, rename, then write cursor). A fresh
    target starts at window 0. An empty window is emitted, never skipped.
    A leftover .tmp_* staging directory is discarded at the next staging.
    Each drip invocation compiles exactly once and re-emits its compile's
    notices.

    Args:
        emit: The open emit (trunk-only).
        config: Validated config; `incremental` must be present.
        out: Warehouse .duckdb file path (duckdb) or drop parent
            directory (csv).
        fmt: Output format.
        anchor: The resolved anchor, or None.
        notice_sink: Receiver for plan notices.

    Returns:
        IncrementalOutcome — status 'emitted' with the window and per-table
        row counts, or 'drained' with neither.

    Raises:
        IncrementalConfigMissing: config.incremental is None.
        IncrementalAnchorRequired / IncrementalPeriodRegimeMismatch:
            cadence regime does not match anchor presence.
        IncrementalFingerprintMismatch: stored fingerprint differs from
            the computed one.
        IncrementalCursorInvalid: per read_cursor.
        ExportError: A windowed business rule fails, or any existing rule.
        ExportRuntimeError: A writer failure; the window's transaction or
            staging directory is rolled back / discarded.
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
            row_counts={},
        )

    # Run the windowed export
    row_counts = export_window(
        emit, config, out, fmt, anchor, window, fingerprint, notice_sink
    )

    # For CSV: write the cursor after the atomic rename
    if fmt == "csv":
        next_cursor = Cursor(
            cursor_format_version=_CURRENT_CURSOR_FORMAT_VERSION,
            fingerprint=fingerprint,
            next_window_index=next_index + 1,
        )
        write_csv_cursor(out, next_cursor)

    return IncrementalOutcome(
        status="emitted",
        window=window,
        row_counts=row_counts,
    )
