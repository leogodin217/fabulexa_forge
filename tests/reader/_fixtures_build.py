"""Fixture builder for base-reader conformance tests.

Synthesizes a spanning-positive v4 emit and several deliberately-broken variants
into a caller-supplied directory. Zero third-party imports — DuckDB + stdlib only.

All builder functions are module-level so they can be tested independently.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import duckdb

from fabulexa_export import SUPPORTED_BASE_FORMAT_VERSION

# ---------------------------------------------------------------------------
# Column lists — match base-format.md exactly (sanitised: no firings, no provenance)
# ---------------------------------------------------------------------------

# history: 6 required base columns only (no written_by_* provenance)
_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# records__actor columns: fixed prefix + prop__ block (no provenance)
_RECORDS_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR", "history_tracked": True},
    # closed-domain status property (in enum_domains)
    {"name": "prop__status", "type": "VARCHAR"},
    # references-annotated FK to doctor kind
    {"name": "prop__doctor_id", "type": "VARCHAR", "references": "doctor"},
    # sub-type discriminator
    {"name": "prop__actor_type", "type": "VARCHAR"},
]

# records__doctor columns: fixed prefix + prop__ block (no provenance)
_RECORDS_DOCTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "prop__name", "type": "VARCHAR"},
]

# membership__actor__appointments columns
_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    # scalar element field
    {"name": "elem__slot", "type": "VARCHAR"},
    # reference element field (two cols)
    {"name": "member__doctor__kind", "type": "VARCHAR"},
    {"name": "member__doctor__id", "type": "VARCHAR"},
]

# record_roles for the spanning fixture: actor is subtyped, doctor is bare
_RECORD_ROLES: dict[str, object] = {
    "actor": {"patient": "fact", "nurse": "fact"},
    "doctor": "dimension",
}

# membership__actor__oncall columns -- build_membership_intervals' new,
# interval-rich membership table (family E's sibling of family C's history)
_ONCALL_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    # nullable scalar element field
    {"name": "elem__note", "type": "VARCHAR"},
    # reference element field (two cols)
    {"name": "member__doctor__kind", "type": "VARCHAR"},
    {"name": "member__doctor__id", "type": "VARCHAR"},
]


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def _col_ddl(col: dict[str, object]) -> str:
    """Build a single column DDL fragment from a column spec dict."""
    name = col["name"]
    ctype = col["type"]
    return f'"{name}" {ctype}'


def _create_table_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement for the given columns."""
    col_fragments = ", ".join(_col_ddl(c) for c in columns)
    return f"CREATE TABLE {table_name} ({col_fragments})"


# ---------------------------------------------------------------------------
# Sidecar builders
# ---------------------------------------------------------------------------


def _table_spec(
    name: str,
    category: str,
    columns: list[dict[str, object]],
    rows: int,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    """Build a table spec dict for a sidecar tables entry."""
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


def _base_sidecar(
    tables: list[dict[str, object]],
    *,
    include_record_roles: bool = True,
) -> dict[str, object]:
    """Build a minimal valid sidecar with the given tables.

    Args:
        tables: Sidecar table spec list.
        include_record_roles: When True, include record_roles in the sidecar.

    Returns:
        A sidecar dict.
    """
    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": tables,
        "runtime": {
            "timezone": "UTC",
            "start_datetime": "2024-01-01T00:00:00+00:00",
        },
        "pinned_ids": {
            "actor": {"alice": "a001"},
        },
        "enum_domains": {
            "actor": {"status": ["active", "discharged", "pending"]},
        },
    }
    if include_record_roles:
        sidecar["record_roles"] = _RECORD_ROLES
    return sidecar


def _spanning_tables(
    history_rows: int,
    records_actor_rows: int,
    records_doctor_rows: int,
    membership_rows: int,
    *,
    actor_columns: list[dict[str, object]] | None = None,
    doctor_columns: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Build the spanning fixture table spec list.

    Args:
        actor_columns: records__actor's column list; defaults to
            `_RECORDS_ACTOR_COLUMNS` (build_spanning's shape).
            `build_history_series` overrides it to additionally mark
            `prop__status` history_tracked and append
            `prop__wait_minutes` / `prop__temperature_c`.
        doctor_columns: records__doctor's column list; defaults to
            `_RECORDS_DOCTOR_COLUMNS` (build_spanning's shape).
            `build_history_series` overrides it to append
            `prop__license_number` / `prop__notes`.
    """
    return [
        _table_spec("history", "fixed", _HISTORY_COLUMNS, history_rows),
        _table_spec(
            "records__actor",
            "records",
            actor_columns if actor_columns is not None else _RECORDS_ACTOR_COLUMNS,
            records_actor_rows,
            record_kind="actor",
        ),
        _table_spec(
            "records__doctor",
            "records",
            doctor_columns if doctor_columns is not None else _RECORDS_DOCTOR_COLUMNS,
            records_doctor_rows,
            record_kind="doctor",
        ),
        _table_spec(
            "membership__actor__appointments",
            "membership",
            _MEMBERSHIP_COLUMNS,
            membership_rows,
            record_kind="actor",
            property_name="appointments",
        ),
    ]


# ---------------------------------------------------------------------------
# DuckDB population helpers
# ---------------------------------------------------------------------------


def _populate_history(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into history (6 base columns, no provenance); return row count."""
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    return 1


def _history_series_actor_columns() -> list[dict[str, object]]:
    """records__actor columns for build_history_series.

    A deep copy of `_RECORDS_ACTOR_COLUMNS` with `prop__status` additionally
    marked `history_tracked`, plus two appended numeric tracked columns:
    `prop__wait_minutes` (BIGINT) and `prop__temperature_c` (DOUBLE). a001
    carries four tracked series (name, status, wait_minutes,
    temperature_c) — `wait_minutes` gives corrupt-recipe scenarios
    (`duplicate_rows` with `jitter`) a `prop__*` column that is
    jitter-eligible (`is_jitter_eligible` requires a non-`*_sim_time`
    `prop__`/`elem__` column typed BIGINT or DOUBLE — no base column
    qualifies); `temperature_c` gives `mutate_cells @ precision_drop` its
    only eligible column (that mutation's type gate is DOUBLE-only — no
    other fixture column, base or appended, is typed DOUBLE). Appending
    rather than editing `_RECORDS_ACTOR_COLUMNS` keeps `build_spanning`'s
    ten-column shape untouched.
    """
    columns = copy.deepcopy(_RECORDS_ACTOR_COLUMNS)
    for col in columns:
        if col["name"] == "prop__status":
            col["history_tracked"] = True
    columns.append(
        {"name": "prop__wait_minutes", "type": "BIGINT", "history_tracked": True}
    )
    columns.append(
        {"name": "prop__temperature_c", "type": "DOUBLE", "history_tracked": True}
    )
    return columns


def _history_series_doctor_columns() -> list[dict[str, object]]:
    """records__doctor columns for build_history_series.

    A deep copy of `_RECORDS_DOCTOR_COLUMNS` with two appended untracked
    VARCHAR columns: `prop__license_number` ("48213", an optional-minus
    all-digit string of >= 4 digits — the only fixture value shaped to
    actually change under `mutate_cells @ format_dirt`; every other VARCHAR
    value in the fixture is short or non-numeric and would hit that
    mutation's no-mutation rule) and `prop__notes` ("café", the only
    fixture value carrying a non-ASCII byte, so it is the only target
    `mutate_cells @ mojibake` can actually mutate — every other VARCHAR
    value is pure ASCII and mojibake's no-mutation rule is identity on
    ASCII). Both are untracked, so a mutation there lands the pure
    `beyond-c1-c12` sentinel — consistent with the shipped stance that
    mojibake/format_dirt (like truncate) inject values whose teaching
    payoff surfaces on downstream export, not in the base layer. Appending
    rather than editing `_RECORDS_DOCTOR_COLUMNS` keeps `build_spanning`'s
    seven-column shape untouched.
    """
    columns = copy.deepcopy(_RECORDS_DOCTOR_COLUMNS)
    columns.append({"name": "prop__license_number", "type": "VARCHAR"})
    columns.append({"name": "prop__notes", "type": "VARCHAR"})
    return columns


def _populate_records_actor_history_series(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert records__actor's row for build_history_series; return row count.

    The same a001 row `_populate_records_actor` writes, plus
    `prop__wait_minutes = 12` and `prop__temperature_c = 37.256` — each
    series' latest pre-slice value (see `_populate_history_series`), so the
    row round-trips (C6-conformant) like every other tracked property here.
    `37.256` carries three decimal digits so a `precision_drop` to any
    smaller digit count is a genuine, non-tie change (never a
    round-half-to-even ambiguity).
    """
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "a001",  # record_id
            10,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            10,  # last_mutation_sim_time
            "Alice",  # prop__name
            "active",  # prop__status
            "d001",  # prop__doctor_id
            "patient",  # prop__actor_type
            12,  # prop__wait_minutes
            37.256,  # prop__temperature_c
        ],
    )
    return 1


def _populate_history_series(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert a multi-event history table; return row count.

    Four series on a001: `name` (5 events — 4 pre-slice ticks plus one
    post-slice tick past slice_at=100, exercising the C6 pre-slice gate),
    `status` (2 events, both pre-slice), `wait_minutes` (2 events, both
    pre-slice — a numeric series backing `prop__wait_minutes`, the
    jitter-eligible column), and `temperature_c` (2 events, both pre-slice
    — a numeric series backing `prop__temperature_c`, the
    precision_drop-eligible DOUBLE column). Every series' latest pre-slice
    value equals its records__actor prop__ cell, so the emit round-trips
    (C6-conformant).
    """
    rows: list[tuple[str, str, str, str, int, str]] = [
        ("trunk", "actor", "a001", "name", 10, "Alice-v0"),
        ("trunk", "actor", "a001", "name", 30, "Alice-v1"),
        ("trunk", "actor", "a001", "name", 60, "Alice-v2"),
        ("trunk", "actor", "a001", "name", 90, "Alice"),
        ("trunk", "actor", "a001", "name", 150, "Alice-future"),
        ("trunk", "actor", "a001", "status", 10, "pending"),
        ("trunk", "actor", "a001", "status", 40, "active"),
        ("trunk", "actor", "a001", "wait_minutes", 20, "5"),
        ("trunk", "actor", "a001", "wait_minutes", 50, "12"),
        ("trunk", "actor", "a001", "temperature_c", 25, "36.5"),
        ("trunk", "actor", "a001", "temperature_c", 55, "37.256"),
    ]
    for row in rows:
        conn.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)", list(row))
    return len(rows)


def _populate_records_actor(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into records__actor; return row count.

    Columns (10 total):
      fork_path, record_id, created_sim_time, active, deactivated_at(NULL),
      last_mutation_sim_time,
      prop__name, prop__status, prop__doctor_id, prop__actor_type
    """
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "a001",  # record_id
            10,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            10,  # last_mutation_sim_time
            "Alice",  # prop__name
            "active",  # prop__status
            "d001",  # prop__doctor_id
            "patient",  # prop__actor_type
        ],
    )
    return 1


def _populate_records_doctor(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into records__doctor; return row count."""
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?)",
        [
            "trunk",  # fork_path
            "d001",  # record_id
            5,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            5,  # last_mutation_sim_time
            "Dr. Smith",  # prop__name
        ],
    )
    return 1


def _populate_records_doctor_history_series(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert records__doctor's rows for build_history_series; return row count.

    The same d001 row `_populate_records_doctor` writes, plus
    `prop__license_number = "48213"` and `prop__notes = "café"` — both
    untracked, so no history series backs them (see
    `_history_series_doctor_columns`) — plus two donor rows
    `mispoint_reference` needs a non-empty donor pool for every fixture
    reference cell (`membership__actor__appointments.member__doctor__id`,
    `records__actor.prop__doctor_id`, both currently d001):

    - d002, an ordinary donor (created_sim_time=6, same shape as d001).
    - d003, created strictly after every write anchor a fixture reference
      cell can produce (`joined_sim_time=10` on the membership row,
      `last_mutation_sim_time=10` on the untracked `prop__doctor_id`) yet
      <= slice_at=100 (created_sim_time=50) — so Phase 2's
      `created_after_reference`-constrained donor pool is non-empty too.

    Both donor rows are untracked like d001, so the emit stays C1-C12
    conformant (no series backs prop__license_number/prop__notes for any
    doctor row).
    """
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d001",  # record_id
            5,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            5,  # last_mutation_sim_time
            "Dr. Smith",  # prop__name
            "48213",  # prop__license_number
            "café",  # prop__notes
        ],
    )
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d002",  # record_id -- ordinary donor
            6,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            6,  # last_mutation_sim_time
            "Dr. Jones",  # prop__name
            "77104",  # prop__license_number
            "clinic",  # prop__notes
        ],
    )
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d003",  # record_id -- late-created donor
            50,  # created_sim_time -- after every write anchor, <= slice_at
            True,  # active
            # deactivated_at = NULL
            50,  # last_mutation_sim_time
            "Dr. Patel",  # prop__name
            "90210",  # prop__license_number
            "annex",  # prop__notes
        ],
    )
    return 3


def _populate_membership(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into membership__actor__appointments; return row count."""
    conn.execute(
        "INSERT INTO membership__actor__appointments VALUES (?, ?, ?, NULL, ?, ?, ?)",
        ["trunk", "a001", 10, "morning", "doctor", "d001"],
    )
    return 1


def _populate_oncall(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into membership__actor__oncall; return row count.

    Covers every family-E population case (see `build_membership_intervals`):
    a002's two adjacent closed intervals (a re-joining member, identical
    element values; A has duration 0 -- only its non-NULL left_sim_time
    matters to `overlap`'s A test; B has duration 10 >= 2 -- an `overlap`
    pair with a closed B, and B independently qualifies for `gap` /
    `left_before_join` too, since a closed interval whose duration clears
    the `overlap` B-boundary threshold necessarily clears those thresholds
    as well); a003's closed (duration 0) then open pair (B's boundary reads
    as slice_at=100, 100 - 15 = 85 >= 2); a004's single closed interval,
    duration 5 (also in the `gap` and `left_before_join` populations); a005's
    single closed interval, left == joined (duration 0 -- filtered from
    every population); a007's lone open interval (in no population); and
    a008's two single-row timelines, identical but for a NULL vs non-NULL
    elem__note (distinct timelines -- NULL groups with NULL, not across;
    left == joined so neither row enters the `gap` / `left_before_join`
    populations -- their sole purpose is timeline distinctness). The `gap`
    and `left_before_join` populations both resolve to exactly
    {a002's B row, a004's row}.
    """
    rows: list[tuple[str, str, int, int | None, str | None, str, str]] = [
        ("trunk", "a002", 10, 10, "day", "doctor", "d001"),
        ("trunk", "a002", 15, 25, "day", "doctor", "d001"),
        ("trunk", "a003", 10, 10, "night", "doctor", "d001"),
        ("trunk", "a003", 15, None, "night", "doctor", "d001"),
        ("trunk", "a004", 10, 15, "single", "doctor", "d001"),
        ("trunk", "a005", 10, 10, "zero", "doctor", "d001"),
        ("trunk", "a007", 50, None, "lone", "doctor", "d001"),
        ("trunk", "a008", 40, 40, None, "doctor", "d001"),
        ("trunk", "a008", 40, 40, "present", "doctor", "d001"),
    ]
    for row in rows:
        conn.execute(
            "INSERT INTO membership__actor__oncall VALUES (?, ?, ?, ?, ?, ?, ?)",
            list(row),
        )
    return len(rows)


def _build_spanning_db(conn: duckdb.DuckDBPyConnection) -> tuple[int, int, int, int]:
    """Create and populate all spanning-fixture tables; return row counts."""
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.execute(_create_table_ddl("records__doctor", _RECORDS_DOCTOR_COLUMNS))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    history_rows = _populate_history(conn)
    actor_rows = _populate_records_actor(conn)
    doctor_rows = _populate_records_doctor(conn)
    membership_rows = _populate_membership(conn)
    return history_rows, actor_rows, doctor_rows, membership_rows


# ---------------------------------------------------------------------------
# Per-fixture builders
# ---------------------------------------------------------------------------


def build_spanning(dest: Path) -> None:
    """Build the spanning fixture into dest.

    A single-branch (trunk-only) sanitised v4 emit with no firings table and
    no provenance columns. Exercises: history (6 base cols), records__actor with
    a references-annotated column, a closed-domain prop__status, a sub-type
    discriminator prop__actor_type, records__doctor, and
    membership__actor__appointments with elem__* and member__*__kind/id columns,
    plus pinned_ids, runtime, enum_domains, and record_roles.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    history_rows, actor_rows, doctor_rows, membership_rows = _build_spanning_db(conn)
    conn.close()

    tables = _spanning_tables(history_rows, actor_rows, doctor_rows, membership_rows)
    sidecar = _base_sidecar(tables, include_record_roles=True)
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_history_series(dest: Path) -> None:
    """Build a spanning-shaped v4 emit whose history carries multi-event series.

    Identical table set, membership rows, branches (slice_at=100), pins,
    enum_domains, and record_roles to build_spanning, with richer records
    rows and a richer history table:

    - records__actor gains two appended numeric tracked columns,
      `prop__wait_minutes` (BIGINT) and `prop__temperature_c` (DOUBLE — the
      fixture's only DOUBLE column, and so the only `mutate_cells @
      precision_drop`-eligible target).
    - records__doctor gains two appended untracked VARCHAR columns,
      `prop__license_number` (an all-digit string shaped for `mutate_cells
      @ format_dirt`) and `prop__notes` (a non-ASCII string shaped for
      `mutate_cells @ mojibake`) — see `_history_series_doctor_columns` for
      why every other fixture VARCHAR value is the wrong shape for either.
    - history carries: at least two distinct series with >= 2 events each,
      one series with >= 4 events (so a random freeze cut has a real
      range), distinct sim_time ticks within each series, at least one
      event with sim_time > slice_at (exercising the C6 pre-slice gate),
      two numeric tracked series (`wait_minutes`, `temperature_c`) backing
      the two numeric prop__ columns above, and every records prop__ cell
      equal to its series' latest pre-slice value (the emit is C1-C12
      conformant — the corrupter's source precondition).

    `enum_domains["actor"]` additionally declares `actor_type` (values
    `nurse`/`patient`, mirroring `record_roles["actor"]`'s declared
    sub-types) -- gives corrupt-recipe scenarios (`mutate_cells @
    out_of_domain`) an enum-domained sub-type discriminator to target, so an
    out-of-domain mutation there deterministically trips the C12 sub-type
    predicate.

    `build_spanning` is not modified — its six consuming suites keep their
    exact fixture.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    actor_columns = _history_series_actor_columns()
    doctor_columns = _history_series_doctor_columns()
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", actor_columns))
    conn.execute(_create_table_ddl("records__doctor", doctor_columns))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )

    history_rows = _populate_history_series(conn)
    actor_rows = _populate_records_actor_history_series(conn)
    doctor_rows = _populate_records_doctor_history_series(conn)
    membership_rows = _populate_membership(conn)
    conn.close()

    tables = _spanning_tables(
        history_rows,
        actor_rows,
        doctor_rows,
        membership_rows,
        actor_columns=actor_columns,
        doctor_columns=doctor_columns,
    )
    sidecar = _base_sidecar(tables, include_record_roles=True)
    enum_domains = sidecar["enum_domains"]
    assert isinstance(enum_domains, dict)
    actor_domains = enum_domains["actor"]
    assert isinstance(actor_domains, dict)
    actor_domains["actor_type"] = ["nurse", "patient"]
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_membership_intervals(dest: Path) -> None:
    """Build a spanning-shaped v4 emit whose membership carries
    interval-rich member timelines.

    Identical history, records__actor, records__doctor,
    membership__actor__appointments rows, branches (slice_at=100), pins,
    enum_domains, and record_roles to build_history_series, plus one new
    membership-category table `membership__actor__oncall` (owner kind
    actor; a `member__doctor__kind` / `member__doctor__id` reference pair
    into records__doctor; at least one nullable elem__ VARCHAR column)
    whose rows cover every family-E population case:

    - one member timeline with two adjacent closed intervals (a re-joining
      member, identical element values) whose successor's duration >= 2 --
      an `overlap` pair with a closed B;
    - one member timeline with a closed interval then an open interval --
      an `overlap` pair whose B boundary reads as slice_at;
    - one single-interval timeline, closed, duration >= 2 -- in the `gap`
      and `left_before_join` populations;
    - one single-interval timeline, closed, left == joined -- filtered from
      every mode's population (duration < 2, no strict inversion source);
    - one lone open interval -- in no mode's population;
    - two single-row timelines identical except a NULL vs non-NULL elem
      value -- distinct timelines (NULL groups with NULL, not across);

    every non-NULL timing value <= slice_at, and the emit is C1-C12
    conformant (the corrupter's source precondition).

    `build_history_series` is not modified -- its consuming suites keep
    their exact fixture.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    actor_columns = _history_series_actor_columns()
    doctor_columns = _history_series_doctor_columns()
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", actor_columns))
    conn.execute(_create_table_ddl("records__doctor", doctor_columns))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    conn.execute(_create_table_ddl("membership__actor__oncall", _ONCALL_COLUMNS))

    history_rows = _populate_history_series(conn)
    actor_rows = _populate_records_actor_history_series(conn)
    doctor_rows = _populate_records_doctor_history_series(conn)
    membership_rows = _populate_membership(conn)
    oncall_rows = _populate_oncall(conn)
    conn.close()

    tables = _spanning_tables(
        history_rows,
        actor_rows,
        doctor_rows,
        membership_rows,
        actor_columns=actor_columns,
        doctor_columns=doctor_columns,
    )
    tables.append(
        _table_spec(
            "membership__actor__oncall",
            "membership",
            _ONCALL_COLUMNS,
            oncall_rows,
            record_kind="actor",
            property_name="oncall",
        )
    )
    sidecar = _base_sidecar(tables, include_record_roles=True)
    enum_domains = sidecar["enum_domains"]
    assert isinstance(enum_domains, dict)
    actor_domains = enum_domains["actor"]
    assert isinstance(actor_domains, dict)
    actor_domains["actor_type"] = ["nurse", "patient"]
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_wrong_version(dest: Path) -> None:
    """Build the wrong_version fixture into dest.

    An otherwise-valid emit whose base_format_version is not 4.
    open_emit raises UnsupportedBaseFormatVersionError on this fixture.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": 99,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_c4_wrong_history_type(dest: Path) -> None:
    """Build the c4_wrong_history_type fixture into dest.

    history col 1 (fork_path) is BIGINT instead of VARCHAR in DuckDB; sidecar
    edited to match the DuckDB (so C2 passes, C4 fails because the type is wrong).
    """
    dest.mkdir(parents=True, exist_ok=True)

    broken_columns = copy.deepcopy(_HISTORY_COLUMNS)
    broken_columns[0] = {"name": "fork_path", "type": "BIGINT"}

    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", broken_columns))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            _table_spec("history", "fixed", broken_columns, 0),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_c5_prop_missing(dest: Path) -> None:
    """Build the c5_prop_missing fixture into dest.

    A prop__* column dropped from the records DuckDB table but still declared
    in the sidecar. C2 + C5 fail.
    """
    dest.mkdir(parents=True, exist_ok=True)

    db_columns = [c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "prop__name"]
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", db_columns))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "records__actor",
                "records",
                _RECORDS_ACTOR_COLUMNS,
                0,
                record_kind="actor",
            ),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_c7_half_null_member(dest: Path) -> None:
    """Build the c7_half_null_member fixture into dest.

    A membership row where member__doctor__kind is set but member__doctor__id
    is NULL. C7 fails because the member reference pair is partially populated.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )

    # member__doctor__kind is non-NULL but member__doctor__id is NULL
    conn.execute(
        "INSERT INTO membership__actor__appointments VALUES (?, ?, ?, NULL, ?, ?, NULL)",
        [
            "trunk",
            "a001",
            10,
            "morning",
            "doctor",  # member__doctor__kind — non-NULL
            # member__doctor__id — NULL (partial pair)
        ],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "membership__actor__appointments",
                "membership",
                _MEMBERSHIP_COLUMNS,
                1,
                record_kind="actor",
                property_name="appointments",
            ),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_schema_mismatch(dest: Path) -> None:
    """Build the schema_mismatch fixture into dest.

    A phantom prop__phantom_column is declared in the sidecar but absent in
    the DuckDB records table. C2 + C5 fail.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.close()

    phantom_columns = copy.deepcopy(_RECORDS_ACTOR_COLUMNS)
    phantom_columns.append({"name": "prop__phantom_column", "type": "VARCHAR"})
    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "records__actor",
                "records",
                phantom_columns,
                0,
                record_kind="actor",
            ),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_history_duplicate_tick(dest: Path) -> None:
    """Build the history_duplicate_tick fixture into dest.

    history has two rows for the same (fork_path, kind, record_id, property, sim_time).
    This is an I3 invariant violation but is outside C1–C12, so validate passes.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))

    for value in ("active", "pending"):
        conn.execute(
            "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
            ["trunk", "actor", "a001", "status", 10, value],
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 2),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_refs_dangling(dest: Path) -> None:
    """Build the refs_dangling fixture into dest.

    A records__actor row has a prop__doctor_id that does not exist in any
    records__doctor table. This is a dangling records-prop reference, which is
    outside C1–C12 (C10 resolves only membership references; C11 checks the
    history_tracked flag, not prop reference integrity). validate passes.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))

    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a001",
            10,
            True,
            10,
            "Alice",
            "active",
            "nonexistent_doctor_id",  # dangling reference
            "patient",
        ],
    )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "records__actor",
                "records",
                _RECORDS_ACTOR_COLUMNS,
                1,
                record_kind="actor",
            ),
        ],
    }
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_c12_missing_kind(dest: Path) -> None:
    """Build the c12_missing_kind fixture into dest.

    record_roles omits the 'doctor' kind even though records__doctor is emitted.
    C12 fails because an emitted records kind is missing from record_roles.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    history_rows, actor_rows, doctor_rows, membership_rows = _build_spanning_db(conn)
    conn.close()

    tables = _spanning_tables(history_rows, actor_rows, doctor_rows, membership_rows)
    sidecar = _base_sidecar(tables, include_record_roles=True)
    # Remove 'doctor' from record_roles so C12 fails
    record_roles = copy.deepcopy(_RECORD_ROLES)
    del record_roles["doctor"]
    sidecar["record_roles"] = record_roles
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def build_c12_missing_subtype(dest: Path) -> None:
    """Build the c12_missing_subtype fixture into dest.

    record_roles["actor"] omits the 'patient' sub-type that is present in
    records__actor data (prop__actor_type = 'patient'). C12 fails because
    an emitted sub-type is not declared in record_roles["actor"].
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    history_rows, actor_rows, doctor_rows, membership_rows = _build_spanning_db(conn)
    conn.close()

    tables = _spanning_tables(history_rows, actor_rows, doctor_rows, membership_rows)
    sidecar = _base_sidecar(tables, include_record_roles=True)
    # Remove 'patient' sub-type from actor so C12 fails
    record_roles: dict[str, object] = {
        "actor": {"nurse": "fact"},  # 'patient' omitted — data has 'patient'
        "doctor": "dimension",
    }
    sidecar["record_roles"] = record_roles
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


# ---------------------------------------------------------------------------
# Top-level: build all fixtures
# ---------------------------------------------------------------------------

_BUILDERS: dict[str, Callable[[Path], None]] = {
    "spanning": build_spanning,
    "history_series": build_history_series,
    "membership_intervals": build_membership_intervals,
    "wrong_version": build_wrong_version,
    "c4_wrong_history_type": build_c4_wrong_history_type,
    "c5_prop_missing": build_c5_prop_missing,
    "c7_half_null_member": build_c7_half_null_member,
    "schema_mismatch": build_schema_mismatch,
    "history_duplicate_tick": build_history_duplicate_tick,
    "refs_dangling": build_refs_dangling,
    "c12_missing_kind": build_c12_missing_kind,
    "c12_missing_subtype": build_c12_missing_subtype,
}


def build_all_fixtures(root: Path) -> dict[str, Path]:
    """Build every fixture into subdirectories of root.

    Args:
        root: Parent directory; each fixture gets its own named subdirectory.

    Returns:
        A mapping of {fixture_name: fixture_path} for all built fixtures.
    """
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name, builder in _BUILDERS.items():
        fixture_path = root / name
        builder(fixture_path)
        result[name] = fixture_path
    return result
