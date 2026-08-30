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
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

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


def build_base_test_emit(
    tmp_path: Path, *, presentation_keys: dict[str, object] | None = None
) -> Path:
    """Build the `patient`-kind test emit for `build_base_render_sql`.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        presentation_keys: An optional sidecar `presentation_keys` block —
            election tests supply a `patient` claim to elect presentation_id;
            omitted (the default) leaves the sidecar exactly as every
            pre-election caller of this fixture expects.

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

    extra: dict[str, object] = {
        "record_roles": {"patient": "dimension"},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
    }
    if presentation_keys is not None:
        extra["presentation_keys"] = presentation_keys

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
        extra=extra,
    )
    return tmp_path


_PATIENT_ELECTION_COLUMNS: list[dict[str, object]] = [
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
        "prop__signup_date",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]


def build_base_render_election_emit(tmp_path: Path) -> Path:
    """Build a `patient`-kind emit for render/date_parse election tests:
    carries a VARCHAR `prop__signup_date` payload column (a date_parse
    candidate) alongside the lifecycle columns a `render` election targets
    (`created_sim_time`, `deactivated_at`).

    - p001: created day 0, deactivated day 2, signup_date '2024-01-15'.
    - p002: created day 0, never deactivated, signup_date NULL (a date_parse
        must let NULL flow through untouched).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_ELECTION_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "p001",
            1001,
            0,
            False,
            2 * DAY_NS,
            2 * DAY_NS,
            0,
            "admitted",
            "2024-01-15",
        ],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p002", 1002, 0, True, 0, 1, "admitted", None],
    )

    for record_id, sim_time, value in (
        ("p001", 0, "admitted"),
        ("p002", 0, "admitted"),
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
                "columns": _PATIENT_ELECTION_COLUMNS,
                "rows": 2,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 2,
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


_VALUE_ELECTION_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__error_rate", "DOUBLE", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__requested_offset_ns",
        "BIGINT",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__opened_at", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__context", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

#: `prop__requested_offset_ns` shares `created_sim_time`'s raw ns value (§
#: render tests: `instant` renders identically to a structural instant of the
#: same value).
_VALUE_ELECTION_OFFSET_NS = 5 * 3600 * 1_000_000_000


def build_base_value_election_emit(tmp_path: Path) -> Path:
    """Build the value-rendering-election render fixture: one `widget`
    records kind, one row, carrying one payload column per new election kind
    (`decimal`: `prop__error_rate` DOUBLE; `instant`: `prop__requested_offset_ns`
    BIGINT, seeded to the same raw ns value as `created_sim_time`;
    `json_precision`: `prop__context` VARCHAR JSON) alongside an unelected
    VARCHAR payload (`prop__opened_at`) whose cast-back-verbatim rendering a
    render test asserts stays unaffected by a sibling column's election.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__widget", _VALUE_ELECTION_WIDGET_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "w001",
            _VALUE_ELECTION_OFFSET_NS,
            True,
            _VALUE_ELECTION_OFFSET_NS,
            0,
            12.3456,
            _VALUE_ELECTION_OFFSET_NS,
            "2024-02-01",
            '{"discount_pct": 0.125, "note": "vip"}',
        ],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": _VALUE_ELECTION_WIDGET_COLUMNS,
                "rows": 1,
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
            "record_roles": {"widget": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_corrupted_presentation_id_patient_emit(tmp_path: Path) -> Path:
    """Build a `patient`-kind emit whose two records share one
    `presentation_id` value — the self-identity guard's target: a corrupted
    elected key must fail `build_base_query_specs` before any writer runs.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    for record_id, record_index in (("p001", 0), ("p002", 1)):
        conn.execute(
            'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            ["trunk", record_id, 999, 0, True, 0, record_index, "admitted", 30],
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
                "rows": 2,
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
            "record_roles": {"patient": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "patient": {
                    "key": {
                        "unique_within": "emit",
                        "branch_stable": False,
                        "slice_stable": False,
                        "key_space": {"class": "counter", "prefix": "", "width": 4},
                    }
                }
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


_TARGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

#: `_TARGET_COLUMNS`'s election-test sibling: carries `presentation_id`, so a
#: `target` election can elect presentation_id (`build_reference_edge_emit`'s
#: `target_presentation_id=True`).
_TARGET_ELECTION_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__lead_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="target",
    ),
    identity_column("ref_index__lead_id", "BIGINT"),
    prop_column(
        "prop__backup_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="target",
    ),
    identity_column("ref_index__backup_id", "BIGINT"),
]


def build_reference_edge_emit(
    tmp_path: Path,
    *,
    target_presentation_id: bool = False,
    presentation_keys: dict[str, object] | None = None,
) -> Path:
    """Build a two-kind emit for reference-edge key-column render tests.

    `target` is a plain dimension kind (no properties, plus `presentation_id`
    when `target_presentation_id=True` — election render tests elect it);
    `actor` carries two reference properties onto `target` (`prop__lead_id`,
    `prop__backup_id`), covering every render-level key/value scenario at a
    mid-tape horizon of `2*DAY_NS + 1`:
      - t001: created day 0, never deactivated — a resolved edge target at
          every horizon. `target_presentation_id=True`: presentation_id
          "T001".
      - t002: created day 0, deactivated day 1 — deactivated before the
          mid-tape horizon; still a resolvable edge target (`active` is
          never a join predicate). presentation_id "T002".
      - t003: created day 3 — created at-or-after the mid-tape horizon, so
          absent from that horizon's index relation; present at the tape's
          end. presentation_id "T003".

      - a001: lead_id=t001 (resolved), backup_id=t002 (resolved despite
          deactivation).
      - a002: lead_id=t999 (dangling — no such target record), backup_id=t001.
      - a003: lead_id=NULL (absent property), backup_id=t001.
      - a004: lead_id=t003 (id present at every horizon; key NULL at the
          mid-tape horizon, resolved at the tape's end).

    Every actor row is created at sim-time 0, well before either horizon
    exercised by the render tests.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        target_presentation_id: Add a `presentation_id` column to `target`
            (values "T001"/"T002"/"T003"), so a `target` election can elect
            presentation_id. False (the default) is byte-identical to this
            fixture's pre-election shape.
        presentation_keys: An optional sidecar `presentation_keys` block —
            a `target` claim, supplied alongside `target_presentation_id`.

    Returns:
        tmp_path (the emit directory).
    """
    target_columns = (
        _TARGET_ELECTION_COLUMNS if target_presentation_id else _TARGET_COLUMNS
    )

    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__target", target_columns))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    if target_presentation_id:
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "t001", "T001", 0, True, 0, 0],
        )
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            ["trunk", "t002", "T002", 0, False, DAY_NS, DAY_NS, 1],
        )
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "t003", "T003", 3 * DAY_NS, True, 3 * DAY_NS, 2],
        )
    else:
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "t001", 0, True, 0, 0],
        )
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, ?, ?)',
            ["trunk", "t002", 0, False, DAY_NS, DAY_NS, 1],
        )
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "t003", 3 * DAY_NS, True, 3 * DAY_NS, 2],
        )

    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, NULL)",
        ["trunk", "a001", 0, True, 0, 0, "t001", "t002"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, NULL)",
        ["trunk", "a002", 0, True, 0, 1, "t999", "t001"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?, NULL)",
        ["trunk", "a003", 0, True, 0, 2, "t001"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, NULL)",
        ["trunk", "a004", 0, True, 0, 3, "t003", "t001"],
    )

    conn.close()

    extra: dict[str, object] = {
        "record_roles": {"target": "dimension", "actor": "dimension"},
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
    }
    if presentation_keys is not None:
        extra["presentation_keys"] = presentation_keys

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__target",
                "category": "records",
                "record_kind": "target",
                "columns": target_columns,
                "rows": 3,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 4,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 0,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 5 * DAY_NS}],
        extra=extra,
    )
    return tmp_path


def build_corrupted_edge_target_emit(tmp_path: Path) -> Path:
    """Build a `target` kind whose two records share one `presentation_id`
    ("T_DUP"), referenced by a single `actor` record — the per-edge guard's
    target: a corrupted elected edge key must fail `build_base_query_specs`
    before any writer runs.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__target", _TARGET_ELECTION_COLUMNS))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    for record_id, record_index in (("t_dup_a", 0), ("t_dup_b", 1)):
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", record_id, "T_DUP", 0, True, 0, record_index],
        )
    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL)",
        ["trunk", "a001", 0, True, 0, 0, "t_dup_a"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__target",
                "category": "records",
                "record_kind": "target",
                "columns": _TARGET_ELECTION_COLUMNS,
                "rows": 2,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
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
            "record_roles": {"target": "dimension", "actor": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "target": {
                    "key": {
                        "unique_within": "emit",
                        "branch_stable": False,
                        "slice_stable": False,
                        "key_space": {"class": "counter", "prefix": "T_", "width": 3},
                    }
                }
            },
        },
    )
    return tmp_path


def build_duplicated_target_emit(tmp_path: Path) -> Path:
    """Build a two-kind emit whose target table carries a row-duplicated
    corrupted shape: `w001`'s `(record_id, record_index)` pair appears twice,
    identically. Exercises the record-index resident's DISTINCT: a
    referencing kind's edge-key join must not fan the spine's row set out.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__target", _TARGET_COLUMNS))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    for _ in range(2):
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, NULL, ?, ?)',
            ["trunk", "w001", 0, True, 0, 0],
        )

    conn.execute(
        'INSERT INTO "records__actor" VALUES'
        " (?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL)",
        ["trunk", "g001", 0, True, 0, 0, "w001"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__target",
                "category": "records",
                "record_kind": "target",
                "columns": _TARGET_COLUMNS,
                "rows": 2,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
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
            "record_roles": {"target": "dimension", "actor": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_base_keys_emit(tmp_path: Path) -> Path:
    """Build a two-kind emit for `declare_keys` engine tests: `patient` carries
    a flat whole-column presentation_keys claim, `doctor` carries a
    partitioned entry whose rollup derives no claim (two counter-class
    sub-types sharing an empty prefix — not pairwise union-safe) — declared
    identity keys only despite carrying a `presentation_keys` entry.

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
    conn.execute(
        'INSERT INTO "records__doctor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "d001", 2001, 0, True, 4 * DAY_NS, 0],
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
                "rows": 1,
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
            "enum_domains": {"doctor": {"doctor_type": enum_options("a", "b")}},
            "presentation_keys": {
                "patient": {
                    "key": {
                        "unique_within": "branch",
                        "branch_stable": True,
                        "slice_stable": True,
                        "key_space": {
                            "class": "record_index",
                            "prefix": "",
                            "width": 4,
                        },
                    }
                },
                "doctor": {
                    "sub_types": {
                        "a": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {"class": "counter", "prefix": "", "width": 3},
                        },
                        "b": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {"class": "counter", "prefix": "", "width": 3},
                        },
                    },
                    # unique_within omitted: both sub-types share an empty
                    # counter prefix and are not pairwise union-safe.
                    "branch_stable": False,
                    "slice_stable": False,
                },
            },
        },
    )
    return tmp_path


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


_TARGET_MIXED_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__target_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__target_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="target",
    ),
    identity_column("ref_index__target_id", "BIGINT"),
]


def build_mixed_edge_election_emit(
    tmp_path: Path, *, corrupt_alpha: bool = False
) -> Path:
    """Build a sub-typed `target` kind (alpha/beta domain, presentation_id
    registry-declared for alpha only) referenced by a flat `widget` kind —
    the excluded mixed-election edge fixture: `target` is excluded from
    base's own output (so its own populations need not elect uniformly, per
    `base.exclude`), but `widget`'s `prop__target_id` edge admits both
    populations under a mixed election (alpha -> presentation_id, beta ->
    record_index — a union-safe pair, `ALPHA_` beside the empty
    record_index prefix), rendering a per-row CASE in one VARCHAR column.

    - w_a1: alpha population, presentation_id "ALPHA_001".
    - w_b1: beta population, presentation_id NULL (undeclared — beta elects
        record_index, never reads the registry).
    - g1: references w_a1 (alpha).
    - g2: references w_b1 (beta).

    Args:
        tmp_path: Directory to write the emit artifacts into.
        corrupt_alpha: When True, adds a second alpha record (`w_a2`) sharing
            `w_a1`'s "ALPHA_001" presentation_id — the per-edge guard's
            target: a corrupted elected key within a proper-subset admitted
            population must fail `build_base_query_specs`.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__target", _TARGET_MIXED_COLUMNS))
    conn.execute(_create_ddl("records__widget", _WIDGET_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w_a1", "ALPHA_001", 0, True, 0, 0, "alpha"],
    )
    if corrupt_alpha:
        conn.execute(
            'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "w_a2", "ALPHA_001", 0, True, 0, 1, "alpha"],
        )
    conn.execute(
        'INSERT INTO "records__target" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w_b1", None, 0, True, 0, 2 if corrupt_alpha else 1, "beta"],
    )

    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)',
        ["trunk", "g1", 0, True, 0, 0, "w_a1"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)',
        ["trunk", "g2", 0, True, 0, 1, "w_b1"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__target",
                "category": "records",
                "record_kind": "target",
                "columns": _TARGET_MIXED_COLUMNS,
                "rows": 3 if corrupt_alpha else 2,
            },
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": _WIDGET_COLUMNS,
                "rows": 2,
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
            "record_roles": {"target": "dimension", "widget": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {"target": {"target_type": enum_options("alpha", "beta")}},
            "presentation_keys": {
                "target": {
                    "sub_types": {
                        "alpha": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "ALPHA_",
                                "width": 3,
                            },
                        },
                    },
                    # A singleton declared set: the rollup equals alpha's own
                    # scalars (the union algebra over one entry).
                    "unique_within": "emit",
                    "branch_stable": False,
                    "slice_stable": False,
                },
            },
        },
    )
    return tmp_path
