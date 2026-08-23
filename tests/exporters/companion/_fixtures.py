"""Shared emit fixture for companion writer tests (artifacts / manifest /
readme). The companion writer reads only sidecar identity facts (version,
branch, runtime) -- never table contents -- so one bare `fixed`-category
table and an empty run.duckdb are all any companion test needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
from _support.sidecar_builder import write_emit

if TYPE_CHECKING:
    from pathlib import Path

_MINIMAL_TABLES: list[dict[str, object]] = [
    {
        "name": "clinic_settings",
        "category": "fixed",
        "rows": 1,
        "columns": [
            {"name": "setting_key", "type": "VARCHAR"},
            {"name": "setting_value", "type": "VARCHAR"},
        ],
    }
]


def write_minimal_emit(
    dest: "Path",
    *,
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Write a minimal one-table emit (sidecar + empty run.duckdb) to `dest`.

    Args:
        dest: The emit directory; base.json and run.duckdb are written inside it.
        branches: Overrides the default single-trunk branch, when given.
        extra: Optional extra top-level sidecar blocks (e.g. `runtime`).
    """
    write_emit(dest, tables=_MINIMAL_TABLES, branches=branches, extra=extra)
    duckdb.connect(str(dest / "run.duckdb")).close()
