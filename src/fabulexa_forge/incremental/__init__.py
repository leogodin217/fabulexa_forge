"""Incremental export driver: window math, fingerprint, cursor, orchestration.

Public names are added phase-by-phase as each phase lands.
"""

from __future__ import annotations

from fabulexa_forge.incremental.cursor import Cursor, read_cursor, write_csv_cursor
from fabulexa_forge.incremental.driver import (
    IncrementalOutcome,
    export_incremental_next,
    export_window,
)
from fabulexa_forge.incremental.fingerprint import compute_fingerprint
from fabulexa_forge.incremental.windows import Window, derive_window, parse_range

__all__ = [
    "Cursor",
    "IncrementalOutcome",
    "Window",
    "compute_fingerprint",
    "derive_window",
    "export_incremental_next",
    "export_window",
    "parse_range",
    "read_cursor",
    "write_csv_cursor",
]
