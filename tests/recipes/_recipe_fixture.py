"""Shared fixture builder for recipe integration tests.

Builds a rich-but-tiny deterministic emit (run.duckdb + base.json) into a
caller-supplied directory. Uses only DuckDB + stdlib — the Fabulexa producer is
never invoked. All values are fixed; no randomness, no clock calls.

The fixture world:
  - records__patient   : history-tracked ``prop__status`` (type-2 capable)
                         + non-tracked ``prop__name`` (type-1)
                         + ``prop__doctor_id`` (references doctor)
                         + ``prop__primary_staff_id`` / ``prop__backup_staff_id``
                           (both reference staff — two edges to one kind, so a
                           patient→staff pathfind is ambiguous without a hint)
  - records__doctor    : type-1 dimension (no history tracking)
  - records__staff     : type-1 dimension with a ``prop__staff_type`` sub-type
                         discriminator: s001 nurse, s002 physician
  - records__admission : identity + lifecycle kind, one type-1 ``prop__ward``,
                         no history. a001 active (create only); a002 deactivated
                         at 2*DAY (create then delete). Exercises the streaming
                         lifecycle (c / d) and the after-only delete tombstone.
  - history            : 4 rows — point-grain status changes only:
                         p001 status: pending@1*DAY, active@2*DAY, discharged@3*DAY
                         p002 status: pending@2*DAY
                         Records carry the current (latest) value of each tracked
                         property: p001.prop__status='discharged' (latest at 3*DAY),
                         p002.prop__status='pending' (latest at 2*DAY).
  - membership__patient__visits : one membership row linking patient p001
                         (open interval — left_sim_time NULL)
  - records__queue     : type-1 dimension owning the ``waiters`` collection;
                         one record q001 ("Triage").
  - membership__queue__waiters : two waiter intervals on owner q001 —
                         p001 closed (joined@1*DAY, left@2*DAY), p002 open
                         (joined@2*DAY, left NULL). Drives the streaming
                         membership-events unpivot: a closed interval yields a
                         join then a leave, an open interval a join only, and the
                         coincident join (p002) / leave (p001) at 2*DAY exercises
                         the join-before-leave order.
  - pinned_ids         : {patient: {alice: p001}}
  - enum_domains       : {patient: {status: [active, discharged, pending]},
                          staff: {staff_type: [nurse, physician]}}
  - record_roles       : {patient: dimension, doctor: dimension,
                          staff: {nurse: dimension, physician: dimension},
                          admission: fact, queue: dimension}
                         staff is sub-typed (object-valued role) — its routing
                         leaf is the ``prop__staff_type`` discriminator, so the
                         streaming routing recipes split it into per-sub-type topics.
  - runtime            : timezone=UTC, start_datetime=2024-01-01T00:00:00+00:00

Time constants (nanosecond offsets from the runtime anchor 2024-01-01T00:00:00Z):
  DAY = 86_400_000_000_000  ns  → one whole day
  1*DAY = 86400000000000    → 2024-01-02
  2*DAY = 172800000000000   → 2024-01-03
  3*DAY = 259200000000000   → 2024-01-04
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

# One whole day in nanoseconds (sim_time unit = ns offset from runtime anchor).
# 1*DAY → 2024-01-02, 2*DAY → 2024-01-03, 3*DAY → 2024-01-04.
DAY: int = 86_400_000_000_000

# ---------------------------------------------------------------------------
# Column specs (sidecar + DDL share the same list)
# ---------------------------------------------------------------------------

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    # history-tracked property — SCD-2 source
    {"name": "prop__status", "type": "VARCHAR", "history_tracked": True},
    # non-tracked property — type-1 source
    {"name": "prop__name", "type": "VARCHAR"},
    # FK to doctor kind
    {"name": "prop__doctor_id", "type": "VARCHAR", "references": "doctor"},
    # Two references to the SAME kind (staff) — an ambiguous patient→staff
    # pathfind that an fk/lookup must disambiguate with an explicit `path` hint.
    {"name": "prop__primary_staff_id", "type": "VARCHAR", "references": "staff"},
    {"name": "prop__backup_staff_id", "type": "VARCHAR", "references": "staff"},
]

_DOCTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
]

_STAFF_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
    # Sub-type discriminator — splits the staff kind into per-type dimensions.
    {"name": "prop__staff_type", "type": "VARCHAR"},
]

_ADMISSION_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    # Identity + lifecycle kind with one type-1 property, no history. Its sole
    # purpose is to exercise the streaming lifecycle: an active record yields a
    # create only; a deactivated record yields a create then a delete tombstone.
    {"name": "prop__ward", "type": "VARCHAR"},
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
    {"name": "elem__slot", "type": "VARCHAR"},
    {"name": "member__doctor__kind", "type": "VARCHAR"},
    {"name": "member__doctor__id", "type": "VARCHAR"},
]

_QUEUE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
]

# Queue waiters: a scalar element field (elem__priority) plus a reference field
# (member__patient__*). Owner is a queue; member is a patient.
_WAITERS_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
    {"name": "member__patient__kind", "type": "VARCHAR"},
    {"name": "member__patient__id", "type": "VARCHAR"},
]

# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def _col_ddl(col: dict[str, object]) -> str:
    """Build a single column DDL fragment from a column spec dict."""
    return f'"{col["name"]}" {col["type"]}'


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement."""
    col_frag = ", ".join(_col_ddl(c) for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_frag})'


def _table_spec(
    name: str,
    category: str,
    columns: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    """Build a sidecar table spec dict."""
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
# Row population
# ---------------------------------------------------------------------------


def _populate_db(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Create all tables and populate rows; return {table_name: row_count}."""
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("records__doctor", _DOCTOR_COLUMNS))
    conn.execute(_create_ddl("records__staff", _STAFF_COLUMNS))
    conn.execute(_create_ddl("records__admission", _ADMISSION_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__patient__visits", _MEMBERSHIP_COLUMNS))
    conn.execute(_create_ddl("records__queue", _QUEUE_COLUMNS))
    conn.execute(_create_ddl("membership__queue__waiters", _WAITERS_COLUMNS))

    # Two patient records.
    # p001: created before first history event (1*DAY); latest history at 3*DAY.
    # p002: created before its first history event (2*DAY); latest history at 2*DAY.
    # prop__primary_staff_id / prop__backup_staff_id both reference staff:
    #   p001 → primary s001, backup s002; p002 → primary s002, backup NULL.
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p001",
            DAY,
            True,
            3 * DAY,
            "discharged",
            "Alice",
            "d001",
            "s001",
            "s002",
        ],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p002",
            2 * DAY,
            True,
            2 * DAY,
            "pending",
            "Bob",
            "d001",
            "s002",
            None,
        ],
    )

    # One doctor record
    conn.execute(
        'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", 50, True, 50, "Dr. Carter"],
    )

    # Two staff records with distinct sub-types (the discriminator-split source).
    conn.execute(
        'INSERT INTO "records__staff" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "s001", 50, True, 50, "Nora Vega", "nurse"],
    )
    conn.execute(
        'INSERT INTO "records__staff" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "s002", 50, True, 50, "Owen Reed", "physician"],
    )

    # Two admission records (identity + lifecycle, no history):
    #   a001 active   — created@1*DAY, never deactivated → stream yields `c` only
    #   a002 closed   — created@1*DAY, deactivated@2*DAY → stream yields `c` then `d`
    conn.execute(
        'INSERT INTO "records__admission" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "a001", DAY, True, DAY, "north"],
    )
    conn.execute(
        'INSERT INTO "records__admission" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", DAY, False, 2 * DAY, 2 * DAY, "south"],
    )

    # History rows (point-grain status changes only).
    # p001 status: pending@1*DAY (2024-01-02), active@2*DAY (2024-01-03),
    #              discharged@3*DAY (2024-01-04) → three SCD-2 versions
    # p002 status: pending@2*DAY (2024-01-03) → one version, open
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", DAY, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", 2 * DAY, "active"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p002", "status", 2 * DAY, "pending"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", 3 * DAY, "discharged"],
    )

    # Membership: patient p001 in a visit slot, doctor d001 as member.
    # joined_sim_time is between p001's first and last history events.
    conn.execute(
        'INSERT INTO "membership__patient__visits" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", 2 * DAY, "morning", "doctor", "d001"],
    )

    # One queue record (owner of the waiters collection), created before any join.
    conn.execute(
        'INSERT INTO "records__queue" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "q001", 50, True, 50, "Triage"],
    )

    # Queue waiters on owner q001 — the membership-events unpivot source:
    #   p001 priority 2: joined@1*DAY, left@2*DAY  → a closed interval (join + leave)
    #   p002 priority 1: joined@2*DAY, left NULL   → an open interval (join only)
    # The leave of p001 and the join of p002 coincide at 2*DAY, so the canonical
    # order places the join (event_class 0) before the leave (event_class 1).
    conn.execute(
        'INSERT INTO "membership__queue__waiters" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "q001", DAY, 2 * DAY, "2", "patient", "p001"],
    )
    conn.execute(
        'INSERT INTO "membership__queue__waiters" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "q001", 2 * DAY, "1", "patient", "p002"],
    )

    return {
        "records__patient": 2,
        "records__doctor": 1,
        "records__staff": 2,
        "records__admission": 2,
        "history": 4,
        "membership__patient__visits": 1,
        "records__queue": 1,
        "membership__queue__waiters": 2,
    }


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_recipe_emit(dest: Path) -> None:
    """Build the recipe fixture emit into dest.

    Writes run.duckdb + base.json. Fully deterministic (no randomness, no
    clock calls). Valid against the vendored contract: ``open_emit(dest)``
    and ``fabulexa-forge validate`` will pass.

    Args:
        dest: Directory to write the emit artifacts into (created if absent).
    """
    dest.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(dest / "run.duckdb"))
    row_counts = _populate_db(conn)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": [
            _table_spec(
                "records__patient",
                "records",
                _PATIENT_COLUMNS,
                row_counts["records__patient"],
                record_kind="patient",
            ),
            _table_spec(
                "records__doctor",
                "records",
                _DOCTOR_COLUMNS,
                row_counts["records__doctor"],
                record_kind="doctor",
            ),
            _table_spec(
                "records__staff",
                "records",
                _STAFF_COLUMNS,
                row_counts["records__staff"],
                record_kind="staff",
            ),
            _table_spec(
                "records__admission",
                "records",
                _ADMISSION_COLUMNS,
                row_counts["records__admission"],
                record_kind="admission",
            ),
            _table_spec(
                "history",
                "fixed",
                _HISTORY_COLUMNS,
                row_counts["history"],
            ),
            _table_spec(
                "membership__patient__visits",
                "membership",
                _MEMBERSHIP_COLUMNS,
                row_counts["membership__patient__visits"],
                record_kind="patient",
                property_name="visits",
            ),
            _table_spec(
                "records__queue",
                "records",
                _QUEUE_COLUMNS,
                row_counts["records__queue"],
                record_kind="queue",
            ),
            _table_spec(
                "membership__queue__waiters",
                "membership",
                _WAITERS_COLUMNS,
                row_counts["membership__queue__waiters"],
                record_kind="queue",
                property_name="waiters",
            ),
        ],
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "pinned_ids": {
            "patient": {"alice": "p001"},
        },
        "enum_domains": {
            "patient": {"status": ["active", "discharged", "pending"]},
            "staff": {"staff_type": ["nurse", "physician"]},
        },
        "record_roles": {
            "patient": "dimension",
            "doctor": "dimension",
            "staff": {"nurse": "dimension", "physician": "dimension"},
            "admission": "fact",
            "queue": "dimension",
        },
    }

    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
