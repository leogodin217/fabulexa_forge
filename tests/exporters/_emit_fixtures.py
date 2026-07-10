"""Emit construction helpers for exporter tests.

Builds minimal test emits for the dimensional exporter. All helpers are
module-level functions — no fixtures — so test modules import directly.

Scenario:
  - records__consultant: entity sub-type (entity_type='consultant')
  - history: journey_instance.state interval series
  - membership__journey_instance__team_members: with elem__role_name, member__entity__{kind,id}
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

# ---------------------------------------------------------------------------
# Sidecar column definitions
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__entity_type", "type": "VARCHAR"},
    {"name": "prop__name", "type": "VARCHAR"},
    {"name": "prop__department", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role_name", "type": "VARCHAR"},
    {"name": "member__entity__kind", "type": "VARCHAR"},
    {"name": "member__entity__id", "type": "VARCHAR"},
]


def _col_ddl(col: dict[str, object]) -> str:
    """Build a single column DDL fragment."""
    return f'"{col["name"]}" {col["type"]}'


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement."""
    col_fragments = ", ".join(_col_ddl(c) for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_fragments})'


def _table_spec(
    name: str,
    category: str,
    columns: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    """Build a table spec dict for a sidecar entry."""
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": columns,
        "rows": rows,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    if property_name is not None:
        spec["property"] = property_name
    return spec


# ---------------------------------------------------------------------------
# Full test emit builder
# ---------------------------------------------------------------------------


def build_test_emit(tmp_path: Path) -> Path:
    """Build a test emit with entity, history, and membership tables.

    Creates:
      - records__entity: two rows (consultant e001 and nurse e002)
      - history: journey_instance.state interval series (two state changes)
      - membership__journey_instance__team_members: one binding per entity

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        _create_ddl("membership__journey_instance__team_members", _MEMBERSHIP_COLUMNS)
    )

    # Two entity rows: one consultant, one nurse
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "e001", True, 10, "consultant", "Dr. Smith", "surgery"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "e002", True, 20, "nurse", "Nurse Joy", "pediatrics"],
    )

    # history: journey_instance.state changes for record j001
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 5, "waiting"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 15, "in_progress"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 25, "completed"],
    )

    # membership: one surgeon binding for journey j001 -> entity e001
    conn.execute(
        'INSERT INTO "membership__journey_instance__team_members" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "j001", 5, "surgeon", "entity", "e001"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _table_spec(
                "records__entity",
                "records",
                _ENTITY_COLUMNS,
                2,
                record_kind="entity",
            ),
            _table_spec(
                "history",
                "fixed",
                _HISTORY_COLUMNS,
                3,
            ),
            _table_spec(
                "membership__journey_instance__team_members",
                "membership",
                _MEMBERSHIP_COLUMNS,
                1,
                record_kind="journey_instance",
                property_name="team_members",
            ),
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "enum_domains": {
            "entity": {"entity_type": ["consultant", "nurse", "admin"]},
        },
    }

    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def build_two_branch_emit(tmp_path: Path) -> Path:
    """Build a two-branch emit for SingleBranch rule testing.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [
            {"fork_path": "trunk", "parent": None, "slice_at": 0},
            {"fork_path": "trunk@branch_a", "parent": "trunk", "slice_at": 50},
        ],
        "tables": [
            _table_spec(
                "records__entity", "records", _ENTITY_COLUMNS, 0, record_kind="entity"
            ),
        ],
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def build_change_log_emit(tmp_path: Path) -> Path:
    """Build a change-log-rich emit for derivations tests.

    Scenario covers all conditions from the design doc's condition table:
      - records__patient: a deactivated record (active=FALSE, deactivated_at set)
      - records__patient: a record whose deactivation collides with a property
        change at the same sim_time (patient p002 at sim_time=50)
      - history: a row with NULL value (property set to None)
      - history: rows for 'audit_event' kind which has no records__ table
        (internal-bookkeeping kind)
      - history table carries an extra provenance column (prov__source)
        that the derivation must not read

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    # History columns include an extra provenance column
    history_columns_with_prov: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
        {"name": "prov__source", "type": "VARCHAR"},  # extra provenance column
    ]

    patient_columns: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "prop__name", "type": "VARCHAR"},
        {"name": "prop__status", "type": "VARCHAR"},
    ]

    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    # Create tables
    conn.execute(_create_ddl("records__patient", patient_columns))
    conn.execute(_create_ddl("history", history_columns_with_prov))

    # p001: active record — no D event
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", True, 30, "Alice", "stable"],
    )
    # p002: deactivated at sim_time=50, which also has a property change at 50
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", False, 50, 50, "Bob", "discharged"],
    )
    # p003: deactivated record with no property-change collision
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p003", False, 80, 80, "Carol", "inactive"],
    )

    # history rows for patient kind
    # p001: name changed at 10 (normal U event)
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "name", 10, "Alice", "system"],
    )
    # p002: status changed at 50 — same instant as deactivation (U before D)
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p002", "status", 50, "discharged", "system"],
    )
    # p001: status is NULL (property set to None — NULL value passthrough)
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", 20, None, "system"],
    )
    # audit_event: history-only kind (no records__ table)
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "audit_event", "ae001", "action", 15, "login", "audit"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "audit_event", "ae002", "action", 35, "logout", "audit"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 200}],
        "tables": [
            _table_spec(
                "records__patient",
                "records",
                patient_columns,
                3,
                record_kind="patient",
            ),
            _table_spec(
                "history",
                "fixed",
                history_columns_with_prov,
                5,
            ),
        ],
    }

    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path


def build_no_runtime_emit(tmp_path: Path) -> Path:
    """Build a test emit without a runtime anchor.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "e001", True, 10, "consultant", "Dr. Smith", "surgery"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 5, "waiting"],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _table_spec(
                "records__entity", "records", _ENTITY_COLUMNS, 1, record_kind="entity"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        "enum_domains": {
            "entity": {"entity_type": ["consultant", "nurse"]},
        },
    }
    (tmp_path / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return tmp_path
