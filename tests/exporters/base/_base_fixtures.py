"""Emit construction helper for base-mode render tests.

Builds a DuckDB-backed emit carrying a single records kind, `patient`,
spanning the horizon and lifecycle cases `build_base_render_sql`'s contract
tests:
  - p001: created at sim-time 0, tracked `prop__status` changing
      "admitted" -> "active" (at 2*DAY) -> "discharged" (at 4*DAY); a
      constant `prop__age`; still active at the tape's end.
  - a002: created at sim-time 0, deactivated at 2*DAY (active=False at the
      tape's end, active=True/deactivated_at=NULL strictly before 2*DAY);
      tracked `prop__status` seeded once ("waiting"), never reasserted.
  - p003: created exactly at 1*DAY — absent from a state-at reconstruction
      whose horizon is 1*DAY (the exclusive `created_sim_time < horizon_ns`
      row filter).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

DAY_NS = 86_400 * 1_000_000_000  # one civil day, in sim-time nanoseconds

_PATIENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__age", "BIGINT", history_tracked=False, temporal_class="constant"
    ),
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement."""
    col_fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_fragments})'


def build_base_test_emit(tmp_path: Path) -> Path:
    """Build the `patient`-kind test emit for `build_base_render_sql`.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    # p001: created day 0, status admitted -> active (day 2) -> discharged
    # (day 4); still active at the tape's end.
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p001", 1001, 0, True, 4 * DAY_NS, 0, "discharged", 30],
    )
    # a002: created day 0, deactivated day 2; status seeded once, never
    # reasserted.
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", 1002, 0, False, 2 * DAY_NS, 2 * DAY_NS, 1, "waiting", 45],
    )
    # p003: created exactly at day 1 — absent from a day-1-horizon reconstruction.
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p003", 1003, DAY_NS, True, DAY_NS, 2, "admitted", 50],
    )

    for record_id, sim_time, value in (
        ("p001", 0, "admitted"),
        ("p001", 2 * DAY_NS, "active"),
        ("p001", 4 * DAY_NS, "discharged"),
        ("a002", 0, "waiting"),
        ("p003", DAY_NS, "admitted"),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "patient", record_id, "status", sim_time, value],
        )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 3,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 5,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 5 * DAY_NS}],
        extra={
            "record_roles": {"patient": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


_DOCTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]


def build_multi_kind_base_emit(tmp_path: Path) -> Path:
    """Build a two-kind emit: `patient` (the render fixture's 3 rows) declared
    first in sidecar order, `doctor` (zero rows) declared second.

    The engine's kind-order and zero-row-still-emitted contract tests: one
    QuerySpec per surviving kind in sidecar declaration order, and a kind
    whose table materializes no rows is still compiled and written.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("records__doctor", _DOCTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p001", 1001, 0, True, 4 * DAY_NS, 0, "discharged", 30],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 1,
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": _DOCTOR_COLUMNS,
                "rows": 0,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 5 * DAY_NS}],
        extra={
            "record_roles": {"patient": "dimension", "doctor": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path
