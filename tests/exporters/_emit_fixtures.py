"""Emit construction helpers for exporter tests.

Builds minimal test emits for the dimensional exporter. All helpers are
module-level functions — no fixtures — so test modules import directly.

Scenario:
  - records__consultant: entity sub-type (entity_type='consultant')
  - history: journey_instance.state interval series
  - membership__journey_instance__team_members: with elem__role_name, member__entity__{kind,id}

Every base.json write routes through `_support.sidecar_builder.write_emit`;
every value-carrying `prop__` column through `prop_column`; every identity
column (`fork_path`, `record_id`, `record_index`) through `identity_column` —
the one sidecar authority for fixture-building test code (design doc §
Fixtures).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

# ---------------------------------------------------------------------------
# Sidecar column definitions
# ---------------------------------------------------------------------------

# records__entity: no history data ever backs entity_type/name/department in
# this fixture, so all three prop__ columns are class 'constant' —
# type-2-eligible values that in fact never change here.
_ENTITY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__entity_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__department",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
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

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
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
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e001", 10, True, 10, 0, "consultant", "Dr. Smith", "surgery"],
    )
    conn.execute(
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e002", 20, True, 20, 1, "nurse", "Nurse Joy", "pediatrics"],
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

    write_emit(
        tmp_path,
        tables=[
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
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {
                "entity": {"entity_type": enum_options("consultant", "nurse", "admin")},
            },
        },
    )
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

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity", "records", _ENTITY_COLUMNS, 0, record_kind="entity"
            ),
        ],
        branches=[
            {"fork_path": "trunk", "parent": None, "slice_at": 0},
            {"fork_path": "trunk@branch_a", "parent": "trunk", "slice_at": 50},
        ],
    )
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

    prop__name and prop__status are class 'tracked' — both genuinely change
    over time in this scenario — so every patient carries the unconditional
    creation-seed genesis row for both at its created_sim_time (10), NULL where
    absent at creation: p001.name's existing sim_time=10 row already opens at
    creation (no addition needed); every other (patient, property) pair gains
    a new genesis row.

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
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
        ),
        prop_column(
            "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
        ),
    ]

    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    # Create tables
    conn.execute(_create_ddl("records__patient", patient_columns))
    conn.execute(_create_ddl("history", history_columns_with_prov))

    # Every patient is created at sim_time=10.
    # p001: active record — no D event
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p001", 10, True, 30, 0, "Alice", "stable"],
    )
    # p002: deactivated at sim_time=50, which also has a property change at 50
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 10, False, 50, 50, 1, "Bob", "discharged"],
    )
    # p003: deactivated record with no property-change collision
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p003", 10, False, 80, 80, 2, "Carol", "inactive"],
    )

    history_rows: list[tuple[str, str, str, str, int, str | None, str]] = [
        # p001: name changed at 10 (normal U event) — already opens at
        # created_sim_time=10, so this row doubles as p001.name's genesis row.
        ("trunk", "patient", "p001", "name", 10, "Alice", "system"),
        # p002: status changed at 50 — same instant as deactivation (U before D)
        ("trunk", "patient", "p002", "status", 50, "discharged", "system"),
        # p001: status is NULL (property set to None — NULL value passthrough)
        ("trunk", "patient", "p001", "status", 20, None, "system"),
        # audit_event: history-only kind (no records__ table)
        ("trunk", "audit_event", "ae001", "action", 15, "login", "audit"),
        ("trunk", "audit_event", "ae002", "action", 35, "logout", "audit"),
        # Unconditional creation-seed genesis rows (§ history, creation-seed
        # guarantee): every history_tracked property of every record, seeded
        # at created_sim_time=10, NULL where absent at creation.
        ("trunk", "patient", "p001", "status", 10, None, "system"),
        ("trunk", "patient", "p002", "name", 10, "Bob", "system"),
        ("trunk", "patient", "p002", "status", 10, None, "system"),
        ("trunk", "patient", "p003", "name", 10, "Carol", "system"),
        ("trunk", "patient", "p003", "status", 10, "inactive", "system"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
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
                len(history_rows),
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 200}],
    )
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
        'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "e001", 10, True, 10, 0, "consultant", "Dr. Smith", "surgery"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "journey_instance", "j001", "state", 5, "waiting"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__entity", "records", _ENTITY_COLUMNS, 1, record_kind="entity"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {
                "entity": {"entity_type": enum_options("consultant", "nurse")},
            },
        },
    )
    return tmp_path
