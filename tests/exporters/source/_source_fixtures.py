"""Emit construction helpers for source-mode exporter tests (renders/engine).

Builds a DuckDB-backed emit spanning all four genres — changelog, reference,
transaction, junction — plus a tracked sub-typed kind (never split). All
helpers are module-level functions — no fixtures — so test modules import
directly.

Scenario:
  - records__visit: tracked (prop__status, prop__priority) -> changelog.
      v001: created only (one 'c' event).
      v002: created, then a coincident status+priority change (one 'u' event).
      v003: created, then deactivated with no property change (one 'd' event).
  - records__shift: tracked, with an untracked prop__shift_type discriminator
      (declared as an enum domain, but never split — tracked dominates)
      -> changelog; one deactivated record ('c' then 'd').
  - records__location: untracked, dimension role -> reference.
  - records__order: untracked, fact role -> transaction; carries a
      reference-annotated prop__location_id column (id-only, unjoined).
  - records__actor: untracked, object-registry role -> splits into
      consultant (dimension) / nurse (fact).
  - membership__visit__team: junction owned by visit; one closed and one
      still-open interval.
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
        "record_roles": {
            "location": "dimension",
            "order": "fact",
            "actor": {"consultant": "dimension", "nurse": "fact"},
        },
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

    Scenario, one kind per genre plus a junction, activity split across all
    three windows:
      - records__visit (changelog, tracked): v001 created in w0, updated in
          w1 ('c' then 'u'); v002 created in w1, deactivated in w2 ('c' then
          'd'); v003 created in w2 only ('c').
      - records__order (transaction): one row's last_mutation_sim_time lands
          in each window.
      - records__location (reference): always a full snapshot regardless of
          window; two rows for realism.
      - membership__visit__team (junction, extract-on-change): m_A joins in
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
            "record_roles": {"location": "dimension", "order": "fact"},
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

    One tracked (changelog-genre) kind, 'widget', anchored at
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
            "record_roles": {"widget": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


_VENUE_TRACKED_COLUMNS: list[dict[str, object]] = [
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

_VENUE_CONSTANT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="constant"
    ),
]


def _build_venue_emit(tmp_path: Path, columns: list[dict[str, object]]) -> Path:
    """Shared body for the venue reclassification fixtures: one dimension-role
    kind, `venue`, whose sole prop__ column is `columns`' single presentation
    value, differing only in its declared temporal_class.
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_ddl("records__venue", columns))
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__venue" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "ven001", 10 * _MS, True, 10 * _MS, 0, "Main Hall"],
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "venue", "ven001", "name", 10 * _MS, "Main Hall"],
    )
    conn.close()

    write_emit(
        tmp_path,
        tables=[
            _table_spec("records__venue", "records", columns, 1, record_kind="venue"),
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 20 * _MS}],
        extra={
            "record_roles": {"venue": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def build_presentation_reclassified_source_emit(tmp_path: Path) -> Path:
    """A dimension-role kind whose sole prop__ column is a `tracked`-class
    presentation value: the genre trichotomy reclassifies it from reference to
    change-log genre even though `record_roles` still declares 'dimension' — a
    name that genuinely changes over time *is* a change log.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    return _build_venue_emit(tmp_path, _VENUE_TRACKED_COLUMNS)


def build_presentation_constant_source_emit(tmp_path: Path) -> Path:
    """The same kind shape as `build_presentation_reclassified_source_emit`,
    presentation column class `constant`: no reclassification — genre stays
    'reference' by role, since the class (not the history_tracked bit) decides.

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    return _build_venue_emit(tmp_path, _VENUE_CONSTANT_COLUMNS)


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
            "record_roles": {"location": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path
