"""Cursor persistence for incremental drip state.

Reads and writes the cursor of record for each output format.
DuckDB: cursor lives in _export_meta + _export_windows (written by the writer
inside the window's transaction). CSV: cursor lives in out/.fabulexa-forge-cursor.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fabulexa_forge.errors import ExportRuntimeError, IncrementalCursorInvalid

_CURSOR_FILE = ".fabulexa-forge-cursor.json"
_CURRENT_CURSOR_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Cursor:
    """Persisted drip position: fingerprint + next window index."""

    cursor_format_version: int
    fingerprint: str
    next_window_index: int


def _read_duckdb_cursor(out: Path) -> Cursor | None:
    """Read the cursor from a DuckDB warehouse file.

    Args:
        out: Warehouse .duckdb file path.

    Returns:
        The stored Cursor, or None for a fresh target.

    Raises:
        IncrementalCursorInvalid: Non-empty catalog without _export_meta.
    """
    import duckdb

    if not out.exists():
        return None

    try:
        conn = duckdb.connect(str(out))
    except Exception as exc:
        raise IncrementalCursorInvalid(
            f"failed to open warehouse DuckDB at {out}: {exc}"
        ) from exc

    try:
        # Check catalog: count all tables and views
        rows = conn.execute("SELECT COUNT(*) FROM information_schema.tables").fetchone()
        total_objects = int(rows[0]) if rows else 0

        if total_objects == 0:
            return None

        # Non-empty catalog: _export_meta must exist
        meta_rows = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables"
            " WHERE table_name = '_export_meta'"
        ).fetchone()
        has_meta = bool(meta_rows and meta_rows[0] > 0)

        if not has_meta:
            raise IncrementalCursorInvalid(
                f"warehouse at {out} has a non-empty catalog but no _export_meta;"
                " not created by --next (use a fresh warehouse)"
            )

        # Read fingerprint from _export_meta
        meta_row = conn.execute(
            "SELECT cursor_format_version, fingerprint FROM _export_meta LIMIT 1"
        ).fetchone()
        if meta_row is None:
            raise IncrementalCursorInvalid(
                f"_export_meta at {out} has no rows; cursor state is corrupt"
            )
        cursor_format_version = int(meta_row[0])
        fingerprint = str(meta_row[1])

        if cursor_format_version != _CURRENT_CURSOR_FORMAT_VERSION:
            raise IncrementalCursorInvalid(
                f"unknown cursor_format_version {cursor_format_version!r}"
                f" in _export_meta at {out}"
            )

        # Compute next_window_index = max(window_index) + 1
        win_row = conn.execute(
            "SELECT MAX(window_index) FROM _export_windows"
        ).fetchone()
        if win_row is None or win_row[0] is None:
            # _export_windows exists but is empty: fresh state after meta was written
            # but before first window (shouldn't happen normally, treat as index 0)
            next_window_index = 0
        else:
            next_window_index = int(win_row[0]) + 1

        return Cursor(
            cursor_format_version=cursor_format_version,
            fingerprint=fingerprint,
            next_window_index=next_window_index,
        )
    finally:
        conn.close()


def _list_non_hidden(out: Path) -> list[Path]:
    """Return non-hidden, non-.tmp_* entries of `out`.

    Hidden entries (names starting with '.') are excluded.
    .tmp_* staging directories are excluded (leftover from a crash).

    Args:
        out: The drop parent directory.

    Returns:
        Sorted list of non-hidden entries.
    """
    return sorted(p for p in out.iterdir() if not p.name.startswith("."))


def _read_csv_cursor(out: Path, window_zero_label: str) -> Cursor | None:
    """Read the cursor from a CSV drop directory.

    Args:
        out: Drop parent directory.
        window_zero_label: The label window 0 would have under the current config.

    Returns:
        The stored Cursor, or None for a fresh target.

    Raises:
        IncrementalCursorInvalid: Lost cursor or structurally invalid state.
    """
    if not out.exists():
        return None

    non_hidden = _list_non_hidden(out)

    # No non-hidden entries: fresh target (dot-entries / .tmp_* leftovers only)
    if not non_hidden:
        return None

    cursor_file = out / _CURSOR_FILE

    # Crash-recovery: exactly one non-hidden entry, a directory named window_zero_label,
    # and the cursor file is absent (drop renamed, cursor write lost).
    if len(non_hidden) == 1 and non_hidden[0].is_dir() and not cursor_file.exists():
        dir_name = non_hidden[0].name
        if dir_name == window_zero_label:
            # Window-0 drop renamed, cursor write lost — re-run restarts at 0
            return None
        else:
            # Mid-crash config change: different label → refuse
            raise IncrementalCursorInvalid(
                f"crash-recovery state: found one drop directory '{dir_name}'"
                f" but expected window-0 label '{window_zero_label}';"
                " the config may have changed mid-drip — resolve manually"
            )

    # Multiple non-hidden entries or cursor file present: cursor file must exist
    if not cursor_file.exists():
        raise IncrementalCursorInvalid(
            f"non-hidden entries exist in {out} but cursor file"
            f" {cursor_file.name!r} is absent; cursor is lost"
        )

    return _parse_csv_cursor_file(cursor_file)


def _parse_csv_cursor_file(cursor_file: Path) -> Cursor:
    """Parse and validate the cursor JSON file.

    Args:
        cursor_file: Path to .fabulexa-forge-cursor.json.

    Returns:
        The parsed Cursor.

    Raises:
        IncrementalCursorInvalid: File is unreadable, invalid JSON, missing fields,
            or has an unknown cursor_format_version.
    """
    try:
        raw = json.loads(cursor_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file} is unreadable or invalid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file} does not contain a JSON object"
        )

    required = {"cursor_format_version", "fingerprint", "next_window_index"}
    missing = required - raw.keys()
    if missing:
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file} is missing required keys: {sorted(missing)}"
        )

    cursor_format_version = raw["cursor_format_version"]
    if not isinstance(cursor_format_version, int):
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file}: cursor_format_version must be an integer,"
            f" got {cursor_format_version!r}"
        )

    if cursor_format_version != _CURRENT_CURSOR_FORMAT_VERSION:
        raise IncrementalCursorInvalid(
            f"unknown cursor_format_version {cursor_format_version!r} in {cursor_file}"
        )

    fingerprint = raw["fingerprint"]
    if not isinstance(fingerprint, str):
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file}: fingerprint must be a string,"
            f" got {fingerprint!r}"
        )

    next_window_index = raw["next_window_index"]
    if not isinstance(next_window_index, int):
        raise IncrementalCursorInvalid(
            f"cursor file {cursor_file}: next_window_index must be an integer,"
            f" got {next_window_index!r}"
        )

    return Cursor(
        cursor_format_version=cursor_format_version,
        fingerprint=fingerprint,
        next_window_index=next_window_index,
    )


def read_cursor(
    out: Path,
    fmt: Literal["csv", "duckdb"],
    window_zero_label: str,
) -> Cursor | None:
    """Read the cursor of record for fmt, classifying fresh vs lost. Pure read.

    Returns None for a fresh target: duckdb — absent file or empty catalog
    (zero tables and views); csv — absent out, no non-hidden entries
    (dot-entries never count), or the one crash-recovery state (exactly one
    non-hidden entry, a directory named exactly window_zero_label — drop
    renamed, first-ever cursor write lost; the re-run overwrites it).
    Otherwise returns the stored cursor: duckdb — _export_meta plus
    next index = max(_export_windows.window_index) + 1; csv —
    out/.fabulexa-forge-cursor.json with keys exactly the Cursor field names.

    Args:
        out: Warehouse .duckdb file path (duckdb) or drop parent
            directory (csv).
        fmt: Output format.
        window_zero_label: The label window 0 derives under the current
            config — classifies the CSV crash-recovery state (a mid-crash
            config change yields a different label and is refused).

    Returns:
        The stored Cursor, or None when the target is fresh (start at
        window 0).

    Raises:
        IncrementalCursorInvalid: Cursor state is unreadable, structurally
            invalid, carries an unknown cursor_format_version, or is lost —
            a non-empty DuckDB catalog without _export_meta, or CSV
            non-hidden entries without the cursor file (beyond the
            crash-recovery state).
    """
    if fmt == "duckdb":
        return _read_duckdb_cursor(out)
    return _read_csv_cursor(out, window_zero_label)


def write_csv_cursor(out: Path, cursor: Cursor) -> None:
    """Write out/.fabulexa-forge-cursor.json (keys = Cursor field names).

    CSV only: the DuckDB cursor of record is written by write_duckdb_window
    inside the window's transaction, never separately.

    Args:
        out: The drop parent directory.
        cursor: The cursor to persist.

    Raises:
        ExportRuntimeError: The write fails.
    """
    cursor_file = out / _CURSOR_FILE
    doc = {
        "cursor_format_version": cursor.cursor_format_version,
        "fingerprint": cursor.fingerprint,
        "next_window_index": cursor.next_window_index,
    }
    try:
        cursor_file.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:
        raise ExportRuntimeError(
            f"failed to write cursor file {cursor_file}: {exc}"
        ) from exc
