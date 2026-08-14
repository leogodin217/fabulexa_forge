"""Emit construction helpers for source-mode exporter tests (renders/engine).

Builds a DuckDB-backed emit spanning tracked, untracked, referencing, and
sub-typed records kinds plus a junction — the raw material a `tables`
declaration list carves into `state` / `junction` output tables. All
helpers are module-level functions — no fixtures — so test modules import
directly.

Scenario (kind -> the declared-table shape a `tables` entry over it takes):
  - records__visit: tracked (prop__status, prop__priority) -> one `state`
      table.
      v001: created only (one 'c' event).
      v002: created, then a coincident status+priority change (one 'u' event).
      v003: created, then deactivated with no property change (one 'd' event).
  - records__shift: tracked, with an untracked prop__shift_type discriminator
      (declared as an enum domain) -> one `state` table, the discriminator
      retained; one deactivated record ('c' then 'd').
  - records__location: untracked -> one `state` table, a full snapshot.
  - records__order: untracked -> one `state` table; carries a
      reference-annotated prop__location_id column (id-only, unjoined).
  - records__actor: untracked, sub-typed (consultant/nurse) -> two `state`
      tables, one declared `sub_types: [consultant]` and one
      `sub_types: [nurse]`.
  - membership__visit__team: junction owned by visit -> one `junction`
      table; one closed and one still-open interval.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.incremental.windows import Window

_MS = 1_000_000  # one "tick" — 1 millisecond in sim-time nanoseconds.

_VISIT_COLUMNS: list[dict[str, object]] = [
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
        "prop__priority", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
]

_SHIFT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__shift_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_LOCATION_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_ORDER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__location_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="location",
    ),
    identity_column("ref_index__location_id", "BIGINT"),
    prop_column(
        "prop__amount", "DOUBLE", history_tracked=False, temporal_class="constant"
    ),
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
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
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
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]


def _col_ddl(col: dict[str, object]) -> str:
    """Build a single column DDL fragment (name + type only)."""
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


def build_source_test_emit(tmp_path: Path, with_runtime: bool = True) -> Path:
    """Build the spanning source-mode test emit.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        with_runtime: Whether the sidecar carries a `runtime` anchor block
            (False builds the SourceAnchorRequired fixture).

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__visit", _VISIT_COLUMNS))
    conn.execute(_create_ddl("records__shift", _SHIFT_COLUMNS))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("records__order", _ORDER_COLUMNS))
    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__visit__team", _MEMBERSHIP_COLUMNS))

    # records__visit: v001 (create only), v002 (create + coincident update),
    # v003 (create + delete, no property change).
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "v001", 1001, 100 * _MS, True, 100 * _MS, 0, "open", 1],
    )
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "v002", 1002, 100 * _MS, True, 150 * _MS, 1, "closed", 5],
    )
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v003", 1003, 100 * _MS, False, 200 * _MS, 200 * _MS, 2, "closed", 9],
    )
    # Creation-seed history rows (contract § history: every history_tracked
    # property is seeded at created_sim_time with its creation value).
    for record_id, status, priority in (
        ("v001", "open", 1),
        ("v002", "open", 1),
        ("v003", "closed", 9),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "visit", record_id, "status", 100 * _MS, status],
        )
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "visit", record_id, "priority", 100 * _MS, str(priority)],
        )
    # v002: status and priority change at the same tick — coalesces into one 'u'.
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "visit", "v002", "status", 150 * _MS, "closed"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "visit", "v002", "priority", 150 * _MS, "5"],
    )

    # records__shift: sh001 — created then deactivated, discriminator retained.
    conn.execute(
        'INSERT INTO "records__shift" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "sh001", 80 * _MS, False, 130 * _MS, 130 * _MS, 0, "day", "closed"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "shift", "sh001", "status", 80 * _MS, "closed"],
    )

    # records__location: loc001 active, loc002 deactivated.
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "loc001", 50 * _MS, True, 50 * _MS, 0, "Ward A", "North"],
    )
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "loc002",
            50 * _MS,
            False,
            120 * _MS,
            120 * _MS,
            1,
            "Ward B",
            "South",
        ],
    )

    # records__order: ord001 references loc001 (id-only, unjoined;
    # ref_index__location_id = loc001's record_index).
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "ord001", 60 * _MS, True, 60 * _MS, 0, "loc001", 0, 250.5],
    )

    # records__actor: act001 consultant, act002 nurse.
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "act001", 70 * _MS, True, 70 * _MS, 0, "consultant", "Dr. Lee"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "act002", 70 * _MS, True, 70 * _MS, 1, "nurse", "Nurse Kim"],
    )

    # membership__visit__team: one closed interval, one still open.
    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v001", 100 * _MS, 150 * _MS, "lead", "actor", "act001"],
    )
    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "v001", 160 * _MS, "support", "actor", "act002"],
    )

    conn.close()

    extra: dict[str, object] = {
        "enum_domains": {
            "actor": {"actor_type": ["consultant", "nurse"]},
            "shift": {"shift_type": ["day", "night"]},
        },
    }
    if with_runtime:
        extra["runtime"] = {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        }

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__visit", "records", _VISIT_COLUMNS, 3, record_kind="visit"
            ),
            _table_spec(
                "records__shift", "records", _SHIFT_COLUMNS, 1, record_kind="shift"
            ),
            _table_spec(
                "records__location",
                "records",
                _LOCATION_COLUMNS,
                2,
                record_kind="location",
            ),
            _table_spec(
                "records__order", "records", _ORDER_COLUMNS, 1, record_kind="order"
            ),
            _table_spec(
                "records__actor", "records", _ACTOR_COLUMNS, 2, record_kind="actor"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 9),
            _table_spec(
                "membership__visit__team",
                "membership",
                _MEMBERSHIP_COLUMNS,
                2,
                record_kind="visit",
                property_name="team",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra=extra,
    )
    return tmp_path


def windowed_test_windows() -> tuple[Window, Window, Window]:
    """The three sim-time windows `build_windowed_source_test_emit`'s activity
    is split across: [0, 100ms), [100ms, 200ms), [200ms, 300ms).
    """
    return (
        Window(index=0, start_ns=0, end_ns=100 * _MS, label="w00000"),
        Window(index=1, start_ns=100 * _MS, end_ns=200 * _MS, label="w00001"),
        Window(index=2, start_ns=200 * _MS, end_ns=300 * _MS, label="w00002"),
    )


def build_windowed_source_test_emit(tmp_path: Path) -> Path:
    """Build a source-mode test emit spanning the three `windowed_test_windows`.

    Scenario, one tracked kind, one untracked referencing kind, one plain
    untracked kind, plus a junction, activity split across all three
    windows:
      - records__visit (tracked -> `state`): v001 created in w0, updated in
          w1 ('c' then 'u'); v002 created in w1, deactivated in w2 ('c' then
          'd'); v003 created in w2 only ('c').
      - records__order (untracked -> `state`): one row's
          last_mutation_sim_time lands in each window.
      - records__location (untracked -> `state`): always a full snapshot
          regardless of window; two rows for realism.
      - membership__visit__team (`junction`, extract-on-change): m_A joins in
          w0 and leaves in w1 (w0 emits join-only, left_at masked; w1
          re-emits with left_at set); m_B joins and leaves both within w2
          (one closed row); m_C joins in w0 and never leaves (w0 emits
          join-only, w1/w2 emit no row for it).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__visit", _VISIT_COLUMNS))
    conn.execute(_create_ddl("records__order", _ORDER_COLUMNS))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__visit__team", _MEMBERSHIP_COLUMNS))

    # records__visit: w0 'c' (v001), w1 'c' (v002) + 'u' (v001), w2 'd' (v002)
    # + 'c' (v003).
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "v001", 2001, 50 * _MS, True, 120 * _MS, 0, "closed", 1],
    )
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v002", 2002, 150 * _MS, False, 250 * _MS, 250 * _MS, 1, "open", 3],
    )
    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "v003", 2003, 220 * _MS, True, 220 * _MS, 2, "pending", 9],
    )
    # Creation-seed history rows.
    for record_id, sim_time, status, priority in (
        ("v001", 50 * _MS, "open", 1),
        ("v002", 150 * _MS, "open", 3),
        ("v003", 220 * _MS, "pending", 9),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "visit", record_id, "status", sim_time, status],
        )
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "visit", record_id, "priority", sim_time, str(priority)],
        )
    # v001: status-only change in w1 -> one coalesced 'u' row.
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "visit", "v001", "status", 120 * _MS, "closed"],
    )

    # records__order: one row's last_mutation_sim_time lands in each window;
    # ref_index__location_id = loc001's record_index (0) throughout.
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "ord001", 60 * _MS, True, 60 * _MS, 0, "loc001", 0, 100.0],
    )
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "ord002", 130 * _MS, True, 130 * _MS, 1, "loc001", 0, 200.0],
    )
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "ord003", 240 * _MS, True, 240 * _MS, 2, "loc001", 0, 300.0],
    )

    # records__location: full snapshot every window regardless.
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "loc001", 50 * _MS, True, 50 * _MS, 0, "Ward A", "North"],
    )
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "loc002",
            150 * _MS,
            False,
            220 * _MS,
            220 * _MS,
            1,
            "Ward B",
            "South",
        ],
    )

    # membership__visit__team: m_A join/leave split across w0/w1, m_B closed
    # within w2, m_C joins in w0 and never leaves.
    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v001", 60 * _MS, 130 * _MS, "lead", "actor", "act001"],
    )
    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v003", 220 * _MS, 260 * _MS, "temp", "actor", "act002"],
    )
    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "v002", 60 * _MS, "support", "actor", "act001"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__visit", "records", _VISIT_COLUMNS, 3, record_kind="visit"
            ),
            _table_spec(
                "records__order", "records", _ORDER_COLUMNS, 3, record_kind="order"
            ),
            _table_spec(
                "records__location",
                "records",
                _LOCATION_COLUMNS,
                2,
                record_kind="location",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 7),
            _table_spec(
                "membership__visit__team",
                "membership",
                _MEMBERSHIP_COLUMNS,
                3,
                record_kind="visit",
                property_name="team",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


_DAY_NS = 86_400 * 1_000_000_000  # one civil day, in sim-time nanoseconds

_WIDGET_COLUMNS: list[dict[str, object]] = [
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
]


def build_day_scale_source_emit(tmp_path: Path) -> Path:
    """Build a `mode: source` emit spanning three calendar-day windows.

    One tracked kind, 'widget' (a `state` table), anchored at
    2024-01-01T00:00:00 UTC: w001 created day 0, w002 created day 1, w001's
    name changes day 2. `slice_at` sits exactly on the day-3 boundary, so
    window index 3 is an empty emitted window and index 4 drains.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__widget", _WIDGET_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    # w001: created day 0 ('c'), name changes day 2 ('u').
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w001", 0, True, 2 * _DAY_NS, 0, "alpha2"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget", "w001", "name", 0, "alpha"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget", "w001", "name", 2 * _DAY_NS, "alpha2"],
    )

    # w002: created day 1 ('c').
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w002", _DAY_NS, True, _DAY_NS, 1, "beta"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "widget", "w002", "name", _DAY_NS, "beta"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__widget",
                "records",
                _WIDGET_COLUMNS,
                2,
                record_kind="widget",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 3),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 3 * _DAY_NS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_empty_source_emit(tmp_path: Path) -> Path:
    """Build a minimal single-table emit whose sole kind materializes zero rows.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__location",
                "records",
                _LOCATION_COLUMNS,
                0,
                record_kind="location",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


_SLICE_ONLY_PATIENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__loyalty_tier",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
]


def build_slice_only_source_emit(tmp_path: Path) -> Path:
    """Build a `mode: source` emit whose sole tracked kind (one `state`
    table) carries one non-exempt `temporal_class: slice_only` property
    alongside a tracked one — the column-projection-only invariance fixture:
    the fold's c/u/d row set and `seq` assignment are unaffected by whether
    `prop__loyalty_tier` is included in the projected column set, since an
    untracked property never drives fold event generation.

    Scenario: p001 created only ('c'); p002 created then its (tracked) status
    changes ('c' then 'u'). Both carry a `prop__loyalty_tier` value set at
    creation and never reasserted (the class forbids it changing).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__patient", _SLICE_ONLY_PATIENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p001", 100 * _MS, True, 100 * _MS, 0, "open", "gold"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "p002", 100 * _MS, True, 150 * _MS, 1, "closed", "silver"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p001", "status", 100 * _MS, "open"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p002", "status", 100 * _MS, "open"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "patient", "p002", "status", 150 * _MS, "closed"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__patient",
                "records",
                _SLICE_ONLY_PATIENT_COLUMNS,
                2,
                record_kind="patient",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 3),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def slice_only_horizon_window() -> Window:
    """The single window spanning `build_slice_only_source_emit`'s whole
    activity — the snapshot render's reconstruction horizon for the Phase-3
    row-set invariance test."""
    return Window(index=0, start_ns=0, end_ns=300 * _MS, label="w00000")


_KEYS_VISIT_COLUMNS: list[dict[str, object]] = [
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
]

_KEYS_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_KEYS_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__actor__kind", "type": "VARCHAR"},
    {"name": "member__actor__id", "type": "VARCHAR"},
]


def build_source_keys_emit(tmp_path: Path) -> Path:
    """Build a `declare_keys` engine-test emit spanning a tracked kind, a
    sub-typed split kind, and a junction.

    - records__visit: tracked (prop__status) -> one `state` table; carries a
        flat whole-column presentation_keys claim; owns
        membership__visit__team (a `junction` table, never keyed).
    - records__actor: untracked, sub-typed (consultant/nurse) -> splits into
        two `state` tables, one per `sub_types` declaration; the block
        declares only `consultant`'s partition — presence is the claim,
        `nurse` gets identity keys only.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__visit", _KEYS_VISIT_COLUMNS))
    conn.execute(_create_ddl("records__actor", _KEYS_ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__visit__team", _KEYS_MEMBERSHIP_COLUMNS))

    conn.execute(
        'INSERT INTO "records__visit" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "v001", 1001, 100 * _MS, True, 100 * _MS, 0, "open"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "visit", "v001", "status", 100 * _MS, "open"],
    )

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "act001", 2001, 70 * _MS, True, 70 * _MS, 0, "consultant"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "act002", 2002, 70 * _MS, True, 70 * _MS, 1, "nurse"],
    )

    conn.execute(
        'INSERT INTO "membership__visit__team" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "v001", 100 * _MS, "actor", "act001"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__visit", "records", _KEYS_VISIT_COLUMNS, 1, record_kind="visit"
            ),
            _table_spec(
                "records__actor", "records", _KEYS_ACTOR_COLUMNS, 2, record_kind="actor"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
            _table_spec(
                "membership__visit__team",
                "membership",
                _KEYS_MEMBERSHIP_COLUMNS,
                1,
                record_kind="visit",
                property_name="team",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "enum_domains": {"actor": {"actor_type": ["consultant", "nurse"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "visit": {
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
                "actor": {
                    "sub_types": {
                        "consultant": {
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
                    "unique_within": "branch",
                    "branch_stable": True,
                    "slice_stable": True,
                },
            },
        },
    )
    return tmp_path


_SLICE_ONLY_ONLY_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__tier",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
]


def build_degenerate_slice_only_source_emit(tmp_path: Path) -> Path:
    """Build a `mode: source` emit whose sole untracked kind (one `state`
    table)'s every property is a non-exempt `temporal_class: slice_only`
    column — the degenerate-unit fixture: the unit is never suppressed,
    still rendering its identity and lifecycle columns with every prop__
    column omitted.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__member", _SLICE_ONLY_ONLY_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__member" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "mem001", 10 * _MS, True, 10 * _MS, 0, "bronze"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__member",
                "records",
                _SLICE_ONLY_ONLY_COLUMNS,
                1,
                record_kind="member",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 20 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Key election fixtures
# ---------------------------------------------------------------------------

_DEVICE_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__device_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_DEVICE_EDGE_ORDER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__device_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="device",
    ),
    identity_column("ref_index__device_id", "BIGINT"),
]

_WATCHERS_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__party__kind", "type": "VARCHAR"},
    {"name": "member__party__id", "type": "VARCHAR"},
]


def build_source_election_emit(tmp_path: Path, *, corrupt_device: bool = False) -> Path:
    """Build the key-election render/engine test emit: a sub-typed tracked
    kind (`device`, day/night, one `state` table per sub-type), a flat
    untracked kind referencing it (`order`, one `state` table), and a
    junction `order` owns (`membership__order__watchers`) whose member field
    admits both kinds.

    - device: dev_day (day, active, presentation_id 'DAY_001'), dev_night
        (night, deactivated at 40ms — its own event-log export renders a
        'd' event, exercising the identity-populated-on-d-rows case).
        `corrupt_device=True` sets dev_night's presentation_id to dev_day's
        value ('DAY_001') — a cross-sub-type duplicate the (spine-unrestricted,
        since device never splits — a change-log kind is never a split unit)
        self-identity guard must catch.
    - order: ord_a references dev_day, ord_b references dev_night —
        `prop__device_id`'s edge dispatches per row on device's own
        records-spine discriminator, resolving ord_b's edge to dev_night's
        elected value despite dev_night's deactivation (never through
        device's fold, which device carries independent of this edge).
    - membership__order__watchers (owner=order): one row's member is
        dev_day (kind='device'), the other ord_a (kind='order') — a
        mixed-kind member field for the `<f>_kind` disambiguator and
        mixed-column type rule tests.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        corrupt_device: Corrupt dev_night's presentation_id to duplicate
            dev_day's — the self/edge guard tests' target.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__device", _DEVICE_COLUMNS))
    conn.execute(_create_ddl("records__order", _DEVICE_EDGE_ORDER_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        _create_ddl("membership__order__watchers", _WATCHERS_MEMBERSHIP_COLUMNS)
    )

    night_presentation_id = "DAY_001" if corrupt_device else "NIGHT_001"

    conn.execute(
        'INSERT INTO "records__device" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "dev_day", "DAY_001", 10 * _MS, True, 10 * _MS, 0, "day", "open"],
    )
    conn.execute(
        'INSERT INTO "records__device" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "dev_night",
            night_presentation_id,
            10 * _MS,
            False,
            40 * _MS,
            40 * _MS,
            1,
            "night",
            "open",
        ],
    )
    for record_id in ("dev_day", "dev_night"):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "device", record_id, "status", 10 * _MS, "open"],
        )

    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "ord_a", "ORD_001", 20 * _MS, True, 20 * _MS, 0, "dev_day", 0],
    )
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "ord_b", "ORD_002", 20 * _MS, True, 20 * _MS, 1, "dev_night", 1],
    )

    conn.execute(
        'INSERT INTO "membership__order__watchers" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "ord_a", 25 * _MS, "device", "dev_day"],
    )
    conn.execute(
        'INSERT INTO "membership__order__watchers" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "ord_b", 25 * _MS, "order", "ord_a"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__device", "records", _DEVICE_COLUMNS, 2, record_kind="device"
            ),
            _table_spec(
                "records__order",
                "records",
                _DEVICE_EDGE_ORDER_COLUMNS,
                2,
                record_kind="order",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
            _table_spec(
                "membership__order__watchers",
                "membership",
                _WATCHERS_MEMBERSHIP_COLUMNS,
                2,
                record_kind="order",
                property_name="watchers",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 50 * _MS}],
        extra={
            "enum_domains": {"device": {"device_type": ["day", "night"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "device": {
                    "sub_types": {
                        "day": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "DAY_",
                                "width": 3,
                            },
                        },
                        "night": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "NIGHT_",
                                "width": 3,
                            },
                        },
                    },
                    "unique_within": "emit",
                    "branch_stable": False,
                    "slice_stable": False,
                },
                "order": {
                    "key": {
                        "unique_within": "emit",
                        "branch_stable": False,
                        "slice_stable": False,
                        "key_space": {
                            "class": "counter",
                            "prefix": "ORD_",
                            "width": 3,
                        },
                    }
                },
            },
        },
    )
    return tmp_path


_TEAM_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_SOLO_DEVICE_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__device_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_TEAM_WATCHERS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "member__thing__kind", "type": "VARCHAR"},
    {"name": "member__thing__id", "type": "VARCHAR"},
]


def build_corrupted_junction_member_emit(tmp_path: Path) -> Path:
    """Build a junction-only emit isolating the per-member-kind guard: a flat
    owner kind (`team`) and a single-sub-type tracked target kind
    (`device`, domain {'solo'}, duplicated presentation_id 'DUP_001' between
    dv1/dv2) admitted only through `membership__team__watchers`' member field
    — no reference-annotated `prop__` column anywhere touches `device`, so a
    corrupted elected key is reachable only via the junction member-edge
    guard, never a reference-edge guard. `device` carries a declared
    sub-type domain (a bare discriminator suffices — the guard's per-kind
    subset only restricts a sub-typed admitted population, per
    `_guard_edge_surface`) so the member-kind guard's subset is non-empty.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__team", _TEAM_COLUMNS))
    conn.execute(_create_ddl("records__device", _SOLO_DEVICE_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__team__watchers", _TEAM_WATCHERS_COLUMNS))

    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "t1", 10 * _MS, True, 10 * _MS, 0],
    )
    for record_id, record_index in (("dv1", 0), ("dv2", 1)):
        conn.execute(
            'INSERT INTO "records__device" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            [
                "trunk",
                record_id,
                "DUP_001",
                10 * _MS,
                True,
                10 * _MS,
                record_index,
                "solo",
                "open",
            ],
        )
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "device", record_id, "status", 10 * _MS, "open"],
        )
    conn.execute(
        'INSERT INTO "membership__team__watchers" VALUES (?, ?, ?, NULL, ?, ?)',
        ["trunk", "t1", 20 * _MS, "device", "dv1"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__team", "records", _TEAM_COLUMNS, 1, record_kind="team"
            ),
            _table_spec(
                "records__device",
                "records",
                _SOLO_DEVICE_COLUMNS,
                2,
                record_kind="device",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
            _table_spec(
                "membership__team__watchers",
                "membership",
                _TEAM_WATCHERS_COLUMNS,
                1,
                record_kind="team",
                property_name="watchers",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 30 * _MS}],
        extra={
            "enum_domains": {"device": {"device_type": ["solo"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "device": {
                    "sub_types": {
                        "solo": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "DUP_",
                                "width": 3,
                            },
                        }
                    },
                    "unique_within": "emit",
                    "branch_stable": False,
                    "slice_stable": False,
                }
            },
        },
    )
    return tmp_path


_SPLIT_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


_TICKET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__ticket_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__priority", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__assignee_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="agent",
    ),
    identity_column("ref_index__assignee_id", "BIGINT"),
]

_AGENT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_WATCHERS_TICKET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__note", "type": "VARCHAR"},
    {"name": "member__party__kind", "type": "VARCHAR"},
    {"name": "member__party__id", "type": "VARCHAR"},
]


def build_events_test_emit(tmp_path: Path) -> Path:
    """Build the event-log render test emit (`events.py`).

    - records__ticket: tracked, sub-typed by `ticket_type` (bug/feature, never
        split — tracked dominates), a reference-annotated `prop__assignee_id`
        column (`references: agent`):
          t001 (bug): created@100ms status=open priority=1 assignee=agent_a;
              status-only update @150ms ("closed"), then priority-only update
              @200ms (5) — two independent update markers, exercising
              "exactly the differing entries" and "coincident changes
              coalesce" (elsewhere, via `visit` in `build_source_test_emit`).
          t002 (bug): created@100ms status=open priority=2 assignee=agent_b;
              no further changes; deactivated@180ms — a destroy whose "last
              value" is the creation after-image.
          t003 (feature): created@120ms status=pending priority=9,
              assignee=NULL; never changes; stays active — excluded when a
              records source narrows to `sub_types: [bug]`.
    - records__agent: flat, untracked: agent_a (record_index 0, 'Alice'),
        agent_b (record_index 1, 'Bob') — the reference-property and
        member-field translation target.
    - membership__ticket__watchers (owner=ticket): one closed interval
        (agent_a, joined 110ms/left 170ms, note 'urgent') and one still-open
        interval (agent_b, joined 180ms, note 'fyi') — a scalar
        (`elem__note`) and a reference (`member__party__kind`/`__id`) field.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__ticket", _TICKET_COLUMNS))
    conn.execute(_create_ddl("records__agent", _AGENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_ddl("membership__ticket__watchers", _WATCHERS_TICKET_COLUMNS))

    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "t001",
            100 * _MS,
            True,
            200 * _MS,
            0,
            "bug",
            "closed",
            5,
            "agent_a",
            0,
        ],
    )
    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "t002",
            100 * _MS,
            False,
            180 * _MS,
            180 * _MS,
            1,
            "bug",
            "open",
            2,
            "agent_b",
            1,
        ],
    )
    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL)',
        ["trunk", "t003", 120 * _MS, True, 120 * _MS, 2, "feature", "pending", 9],
    )

    for record_id, sim_time, status, priority in (
        ("t001", 100 * _MS, "open", 1),
        ("t002", 100 * _MS, "open", 2),
        ("t003", 120 * _MS, "pending", 9),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "ticket", record_id, "status", sim_time, status],
        )
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "ticket", record_id, "priority", sim_time, str(priority)],
        )
    # t001: status-only change, then a later priority-only change.
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "ticket", "t001", "status", 150 * _MS, "closed"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "ticket", "t001", "priority", 200 * _MS, "5"],
    )

    conn.execute(
        'INSERT INTO "records__agent" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "agent_a", 50 * _MS, True, 50 * _MS, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__agent" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "agent_b", 50 * _MS, True, 50 * _MS, 1, "Bob"],
    )

    conn.execute(
        'INSERT INTO "membership__ticket__watchers" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t001", 110 * _MS, 170 * _MS, "urgent", "agent", "agent_a"],
    )
    conn.execute(
        'INSERT INTO "membership__ticket__watchers" VALUES (?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "t001", 180 * _MS, "fyi", "agent", "agent_b"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__ticket", "records", _TICKET_COLUMNS, 3, record_kind="ticket"
            ),
            _table_spec(
                "records__agent", "records", _AGENT_COLUMNS, 2, record_kind="agent"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 8),
            _table_spec(
                "membership__ticket__watchers",
                "membership",
                _WATCHERS_TICKET_COLUMNS,
                2,
                record_kind="ticket",
                property_name="watchers",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "enum_domains": {"ticket": {"ticket_type": ["bug", "feature"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_event_tie_test_emit(tmp_path: Path) -> Path:
    """Build an emit whose sole record's update and deactivation coincide.

    The event-log render derives each before-image from a per-record LAG over
    the row-state-events fold. A record that changes and is deactivated at the
    SAME sim_time yields two fold events at one instant, so the lag window's
    ORDER BY must break the tie on `event_class` — ordering on
    `event_sim_time` alone leaves the two orderable either way, and the swap
    silently corrupts both before-images (the update reads the destroy's
    nulled after-image, the destroy reads the pre-update value).

    - records__ticket: t900 (bug), tracked. created@100ms status=open
        priority=1; status changes to 'closed' @150ms; deactivated@150ms —
        the tie. The only correct chain is create [None, 'open'],
        update ['open', 'closed'], destroy ['closed', None].
    - records__agent: agent_a, the reference target of prop__assignee_id.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__ticket", _TICKET_COLUMNS))
    conn.execute(_create_ddl("records__agent", _AGENT_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "t900",
            100 * _MS,
            False,
            150 * _MS,
            150 * _MS,
            0,
            "bug",
            "closed",
            1,
            "agent_a",
            0,
        ],
    )
    for property_name, sim_time, value in (
        ("status", 100 * _MS, "open"),
        ("priority", 100 * _MS, "1"),
        ("status", 150 * _MS, "closed"),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "ticket", "t900", property_name, sim_time, value],
        )

    conn.execute(
        'INSERT INTO "records__agent" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "agent_a", 50 * _MS, True, 50 * _MS, 0, "Alice"],
    )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__ticket", "records", _TICKET_COLUMNS, 1, record_kind="ticket"
            ),
            _table_spec(
                "records__agent", "records", _AGENT_COLUMNS, 1, record_kind="agent"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 3),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "enum_domains": {"ticket": {"ticket_type": ["bug", "feature"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_event_log_suppressed_update_test_emit(tmp_path: Path) -> Path:
    """Build an emit whose sole audited property is once reasserted at its
    current value — a genuine no-op history write.

    The row-state-events fold fires a 'u' event at every distinct history
    sim_time of an audited property, whether or not the value actually
    changed. When it does not, the event-log render's diff drops the whole
    row (empty `changes`), and `id`'s ROW_NUMBER — assigned beneath the
    window predicate and the arm's own suppression filter — must stay dense
    across the drop: the surviving rows' `id` values are still consecutive
    integers, with no gap left where the suppressed row would have sat.

    - records__ticket: t600 (bug), tracked status. created@100ms status=open
        priority=1; status changes to 'closed'@150ms (a real change, kept);
        status reasserted 'closed'@180ms (no-op — old and new both
        'closed', dropped); deactivated@250ms. t601 (bug): created@120ms
        status=pending priority=9; never changes; stays active.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_create_ddl("records__ticket", _TICKET_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "t600",
            100 * _MS,
            False,
            250 * _MS,
            180 * _MS,
            0,
            "bug",
            "closed",
            1,
            None,
            None,
        ],
    )
    conn.execute(
        'INSERT INTO "records__ticket" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)',
        [
            "trunk",
            "t601",
            120 * _MS,
            True,
            120 * _MS,
            1,
            "bug",
            "pending",
            9,
            None,
            None,
        ],
    )

    for record_id, property_name, sim_time, value in (
        ("t600", "status", 100 * _MS, "open"),
        ("t600", "priority", 100 * _MS, "1"),
        ("t600", "status", 150 * _MS, "closed"),
        ("t600", "status", 180 * _MS, "closed"),
        ("t601", "status", 120 * _MS, "pending"),
        ("t601", "priority", 120 * _MS, "9"),
    ):
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
            ["trunk", "ticket", record_id, property_name, sim_time, value],
        )

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__ticket", "records", _TICKET_COLUMNS, 2, record_kind="ticket"
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 6),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "enum_domains": {"ticket": {"ticket_type": ["bug", "feature"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_split_actor_presentation_id_emit(
    tmp_path: Path, *, duplicate_within_consultant: bool = False
) -> Path:
    """Build a split `actor` kind (consultant/nurse) for the split-unit
    identity guard's spine-restriction test: consultant's c1 and nurse's n1
    coincidentally share one presentation_id value ('CONS_001') — a
    cross-population value collision each sub-type's own restricted-spine
    guard must NOT catch, since consultant and nurse are separate output
    tables. `duplicate_within_consultant=True` adds a second consultant
    record (c2) sharing c1's value — a genuine duplicate WITHIN consultant's
    own spine, which the guard must still catch.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        duplicate_within_consultant: Add a second consultant record sharing
            c1's presentation_id.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__actor", _SPLIT_ACTOR_COLUMNS))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))

    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "c1", "CONS_001", 0, True, 0, 0, "consultant"],
    )
    next_index = 1
    if duplicate_within_consultant:
        conn.execute(
            'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "c2", "CONS_001", 0, True, 0, next_index, "consultant"],
        )
        next_index += 1
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "n1", "CONS_001", 0, True, 0, next_index, "nurse"],
    )
    rows = next_index + 1

    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _SPLIT_ACTOR_COLUMNS,
                rows,
                record_kind="actor",
            ),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {"actor": {"actor_type": ["consultant", "nurse"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "presentation_keys": {
                "actor": {
                    "sub_types": {
                        "consultant": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "CONS_",
                                "width": 3,
                            },
                        },
                        "nurse": {
                            "unique_within": "emit",
                            "branch_stable": False,
                            "slice_stable": False,
                            "key_space": {
                                "class": "counter",
                                "prefix": "NURSE_",
                                "width": 3,
                            },
                        },
                    },
                    "unique_within": "emit",
                    "branch_stable": False,
                    "slice_stable": False,
                },
            },
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Junction owner selection: a sub-typed owner ('worker', day/night) with a
# constant `prop__region` property, owning `membership__worker__ward`, one
# interval per owner — source-row-selection sprint § Phase 2, the parent
# lookup's junction render.
# ---------------------------------------------------------------------------

_WORKER_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__worker_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_WARD_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__desk", "type": "VARCHAR"},
]


def build_source_junction_selection_emit(tmp_path: Path) -> Path:
    """Build a source-mode emit for junction owner selection: two `worker`
    owners split day/night and by `prop__region`, each with one
    `membership__worker__ward` interval, activity spanning two windows.

    - w1 (day, region=east): interval joined 60ms, left 120ms (closed,
        window 0).
    - w2 (night, region=west): interval joined 130ms, left NULL (still
        open, window 1).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__worker", _WORKER_COLUMNS))
    conn.execute(_create_ddl("membership__worker__ward", _WARD_COLUMNS))

    conn.execute(
        'INSERT INTO "records__worker" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "w1", 50 * _MS, True, 50 * _MS, 0, "day", "east"],
    )
    conn.execute(
        'INSERT INTO "records__worker" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "w2", 50 * _MS, True, 50 * _MS, 1, "night", "west"],
    )
    conn.execute(
        'INSERT INTO "membership__worker__ward" VALUES (?, ?, ?, ?, ?)',
        ["trunk", "w1", 60 * _MS, 120 * _MS, "A"],
    )
    conn.execute(
        'INSERT INTO "membership__worker__ward" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "w2", 130 * _MS, "B"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec(
                "records__worker", "records", _WORKER_COLUMNS, 2, record_kind="worker"
            ),
            _table_spec(
                "membership__worker__ward",
                "membership",
                _WARD_COLUMNS,
                2,
                record_kind="worker",
                property_name="ward",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        extra={
            "enum_domains": {"worker": {"worker_type": ["day", "night"]}},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path
