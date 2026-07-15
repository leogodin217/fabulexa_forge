"""Shared emit-building scaffold for row_state_events / state_at derivation tests.

Both `test_row_state_events.py` and `test_state_at.py` materialize their SQL
against minimal in-process emits built from the same sidecar shape
(`records__<kind>` + `history`). This module holds that shared scaffold so the
shape never drifts between the two test files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
from _support.sidecar_builder import prop_column, write_emit

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__score", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

# Interleaved: tracked (alpha), current (beta), tracked (gamma) — declaration order
_RECORD_COLS_INTERLEAVED: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    prop_column(
        "prop__alpha", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__beta", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
    prop_column(
        "prop__gamma", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

# With presentation_id and interleaved props
_RECORD_COLS_INTERLEAVED_WITH_PID: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    prop_column(
        "prop__alpha", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__beta", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
    prop_column(
        "prop__gamma", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_RECORD_COLS_WITH_PID: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# Emit builder helpers
# ---------------------------------------------------------------------------


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    return spec


def _build_emit(
    tmp_path: Path,
    record_rows: list[tuple[Any, ...]],
    history_rows: list[tuple[Any, ...]],
    kind: str = "item",
    record_cols: list[dict[str, object]] | None = None,
) -> Path:
    """Build a minimal v5 emit with records__<kind> and history tables."""
    if record_cols is None:
        record_cols = _RECORD_COLS

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl(f"records__{kind}", record_cols))
    conn.execute(_ddl("history", _HISTORY_COLS))

    col_placeholders = ", ".join("?" for _ in record_cols)
    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__{kind}" VALUES ({col_placeholders})',
            list(row),
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                f"records__{kind}",
                "records",
                record_cols,
                len(record_rows),
                record_kind=kind,
            ),
            _table_spec("history", "fixed", _HISTORY_COLS, len(history_rows)),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path
