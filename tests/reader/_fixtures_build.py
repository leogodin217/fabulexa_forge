"""Fixture builder for base-reader conformance tests.

Synthesizes a spanning-positive emit and several deliberately-broken variants
into a caller-supplied directory. Every base.json write routes through
`_support.sidecar_builder.write_emit`; every value-carrying `prop__` column is
built through `prop_column`; every identity column (`fork_path`, `record_id`,
`record_index`, `ref_index__<name>`) is built through `identity_column`.

All builder functions are module-level so they can be tested independently.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import duckdb
from _support.sidecar_builder import (
    UNSUPPORTED_VERSION_SENTINEL,
    identity_column,
    prop_column,
    write_emit,
)

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

# records__actor columns: fixed prefix + record_index + prop__/ref_index__ block
# (no provenance). prop__doctor_id is reference-annotated, so its
# ref_index__doctor_id sibling immediately follows it (§ Dense record index).
_RECORDS_ACTOR_COLUMNS: list[dict[str, object]] = [
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
    # closed-domain status property (in enum_domains); mutable but not
    # history-tracked in this fixture's spanning shape (build_history_series
    # flips it to tracked) -- the fixture's sole slice_only column
    prop_column(
        "prop__status", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
    # references-annotated FK to doctor kind, paired with its ref_index__ sibling
    prop_column(
        "prop__doctor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="doctor",
    ),
    identity_column("ref_index__doctor_id", "BIGINT"),
    # sub-type discriminator -- fixed at creation, never changes
    prop_column(
        "prop__actor_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]

# records__doctor columns: fixed prefix + record_index + prop__ block (no provenance)
_RECORDS_DOCTOR_COLUMNS: list[dict[str, object]] = [
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

# membership__actor__appointments columns (no ref_index analog: membership
# reference pairs stay member__<name>__kind/member__<name>__id)
_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
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
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
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


_SPANNING_BRANCHES: list[dict[str, object]] = [
    {"fork_path": "trunk", "parent": None, "slice_at": 100}
]


def _base_extra(*, include_record_roles: bool = True) -> dict[str, object]:
    """Build the extra top-level sidecar blocks shared by spanning-shaped fixtures.

    Args:
        include_record_roles: When True, include record_roles in the result.

    Returns:
        A dict suitable for write_emit's `extra` argument.
    """
    extra: dict[str, object] = {
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
        extra["record_roles"] = _RECORD_ROLES
    return extra


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
    """Insert rows into history (6 base columns, no provenance); return row count.

    a001's genesis row for the history-tracked `prop__name` (C13's per-record
    sample).
    """
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    return 1


def _populate_history_a002_genesis(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert a002's `name` genesis row into history; return row count.

    a002 is the spanning fixture's NULL-doctor-reference row
    (`_populate_records_actor`) -- C13 samples every records row of a kind
    carrying a history-tracked column, so it too needs its own genesis row.
    """
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a002", "name", 15, "Bob"],
    )
    return 1


def _history_series_actor_columns() -> list[dict[str, object]]:
    """records__actor columns for build_history_series.

    A deep copy of `_RECORDS_ACTOR_COLUMNS` with `prop__status` additionally
    marked `history_tracked` + `temporal_class="tracked"` (its spanning-shape
    class is `slice_only`), plus two appended numeric tracked columns:
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
            col["temporal_class"] = "tracked"
    columns.append(
        prop_column(
            "prop__wait_minutes",
            "BIGINT",
            history_tracked=True,
            temporal_class="tracked",
        )
    )
    columns.append(
        prop_column(
            "prop__temperature_c",
            "DOUBLE",
            history_tracked=True,
            temporal_class="tracked",
        )
    )
    return columns


def _history_series_doctor_columns() -> list[dict[str, object]]:
    """records__doctor columns for build_history_series and
    build_membership_intervals.

    A deep copy of `_RECORDS_DOCTOR_COLUMNS` with three appended columns:
    `prop__license_number` ("48213", an optional-minus all-digit string of
    >= 4 digits — the only fixture value shaped to actually change under
    `mutate_cells @ format_dirt`; every other VARCHAR value in the fixture is
    short or non-numeric and would hit that mutation's no-mutation rule),
    `prop__notes` ("café", the only fixture value carrying a non-ASCII byte,
    so it is the only target `mutate_cells @ mojibake` can actually mutate —
    every other VARCHAR value is pure ASCII and mojibake's no-mutation rule
    is identity on ASCII), and `prop__specialty` (history-tracked — doctor's
    only tracked series, backed by a genesis-only, non-NULL history row per
    doctor in `_populate_history_series`; gives `hard-deleted-parents`'
    `hard_delete_referenced_doctor` a tracked series to orphan, so that
    defect's `impact` carries C6 alongside C10). `license_number` is a
    once-granted identifier (class `constant`); `notes` is an editable
    free-text field (class `slice_only`); neither is history_tracked.
    Appending rather than editing `_RECORDS_DOCTOR_COLUMNS` keeps
    `build_spanning`'s seven-column shape untouched.
    """
    columns = copy.deepcopy(_RECORDS_DOCTOR_COLUMNS)
    columns.append(
        prop_column(
            "prop__license_number",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        )
    )
    columns.append(
        prop_column(
            "prop__notes", "VARCHAR", history_tracked=False, temporal_class="slice_only"
        )
    )
    columns.append(
        prop_column(
            "prop__specialty", "VARCHAR", history_tracked=True, temporal_class="tracked"
        )
    )
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
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "a001",  # record_id
            10,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            10,  # last_mutation_sim_time
            0,  # record_index
            "Alice",  # prop__name
            "active",  # prop__status
            "d001",  # prop__doctor_id
            0,  # ref_index__doctor_id -- d001's record_index
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
    `status` (2 events, both pre-slice, the first coincident with
    created_sim_time=10 -- its own genesis row), `wait_minutes` (3 events,
    all pre-slice: an unconditional NULL-valued genesis row at
    created_sim_time=10 -- `prop__wait_minutes` has no value until its
    first post-creation tick -- plus the same two ticks as before), and
    `temperature_c` (3 events, all pre-slice: a NULL-valued genesis row at
    created_sim_time=10 plus the same two ticks as before) — the two
    numeric series backing `prop__wait_minutes` / `prop__temperature_c`,
    the jitter-eligible and precision_drop-eligible columns respectively.
    Every series' latest pre-slice value equals its records__actor prop__
    cell, so the emit round-trips (C6-conformant); the added genesis rows
    are earlier than every existing tick and never become the latest
    pre-slice value.

    Also one genesis-only `specialty` series per doctor (d001/d002/d003),
    each a single non-NULL row at its own created_sim_time equal to that
    doctor's records__doctor `prop__specialty` cell -- a specialty is fixed
    at creation, so the genesis row is the whole series (C6-conformant, no
    later tick needed). d001's is the series `hard-deleted-parents`'
    `hard_delete_referenced_doctor` orphans.
    """
    rows: list[tuple[str, str, str, str, int, str | None]] = [
        ("trunk", "actor", "a001", "name", 10, "Alice-v0"),
        ("trunk", "actor", "a001", "name", 30, "Alice-v1"),
        ("trunk", "actor", "a001", "name", 60, "Alice-v2"),
        ("trunk", "actor", "a001", "name", 90, "Alice"),
        ("trunk", "actor", "a001", "name", 150, "Alice-future"),
        ("trunk", "actor", "a001", "status", 10, "pending"),
        ("trunk", "actor", "a001", "status", 40, "active"),
        ("trunk", "actor", "a001", "wait_minutes", 10, None),  # genesis, no value yet
        ("trunk", "actor", "a001", "wait_minutes", 20, "5"),
        ("trunk", "actor", "a001", "wait_minutes", 50, "12"),
        ("trunk", "actor", "a001", "temperature_c", 10, None),  # genesis, no value yet
        ("trunk", "actor", "a001", "temperature_c", 25, "36.5"),
        ("trunk", "actor", "a001", "temperature_c", 55, "37.256"),
        ("trunk", "doctor", "d001", "specialty", 5, "cardiology"),  # genesis
        ("trunk", "doctor", "d002", "specialty", 6, "radiology"),  # genesis
        ("trunk", "doctor", "d003", "specialty", 50, "surgery"),  # genesis
    ]
    for row in rows:
        conn.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)", list(row))
    return len(rows)


def _populate_records_actor(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into records__actor; return row count.

    Columns (12 total): fork_path, record_id, created_sim_time, active,
    deactivated_at(NULL), last_mutation_sim_time, record_index, prop__name,
    prop__status, prop__doctor_id, ref_index__doctor_id, prop__actor_type.

    Two rows: a001 (references d001 -- `ref_index__doctor_id` resolves to
    d001's record_index) and a002 (a NULL-together pair -- both
    `prop__doctor_id` and `ref_index__doctor_id` NULL, exercising the pair's
    NULL-together invariant on a genuinely unreferenced actor).
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
            0,  # record_index
            "Alice",  # prop__name
            "active",  # prop__status
            "d001",  # prop__doctor_id
            0,  # ref_index__doctor_id -- d001's record_index
            "patient",  # prop__actor_type
        ],
    )
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, ?)",
        [
            "trunk",  # fork_path
            "a002",  # record_id
            15,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            15,  # last_mutation_sim_time
            1,  # record_index
            "Bob",  # prop__name
            "active",  # prop__status
            # prop__doctor_id = NULL, ref_index__doctor_id = NULL (NULL-together)
            "nurse",  # prop__actor_type
        ],
    )
    return 2


def _populate_records_doctor(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert rows into records__doctor; return row count.

    Three doctors mixing record_id shapes -- d001 (a hex-valid alnum id),
    "1005" (a pure decimal-string id), and "9f2ab1" (a hex-digest id) -- so no
    id/index-conflating implementation can resolve `ref_index__doctor_id` by
    coincidentally parsing `record_id` as an integer.
    """
    rows = [
        ("d001", 5, "Dr. Smith"),
        ("1005", 6, "Dr. Numeric"),
        ("9f2ab1", 7, "Dr. Hex"),
    ]
    for record_index, (record_id, created_sim_time, name) in enumerate(rows):
        conn.execute(
            "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            [
                "trunk",  # fork_path
                record_id,  # record_id
                created_sim_time,  # created_sim_time
                True,  # active
                # deactivated_at = NULL
                created_sim_time,  # last_mutation_sim_time
                record_index,  # record_index
                name,  # prop__name
            ],
        )
    return len(rows)


def _populate_records_doctor_history_series(conn: duckdb.DuckDBPyConnection) -> int:
    """Insert records__doctor's rows for build_history_series; return row count.

    The same d001 row `_populate_records_doctor` writes, plus
    `prop__license_number = "48213"`, `prop__notes = "café"` (both untracked,
    so no history series backs them — see `_history_series_doctor_columns`),
    and `prop__specialty` — history-tracked, backed by a genesis-only,
    non-NULL history row per doctor in `_populate_history_series` (each
    doctor's specialty is fixed at creation, so its single tracked event IS
    its genesis row; the emit stays C6-conformant) — plus two donor rows
    `mispoint_reference` needs a non-empty donor pool for every fixture
    reference cell (`membership__actor__appointments.member__doctor__id`,
    `records__actor.prop__doctor_id`, both currently d001):

    - d002, an ordinary donor (created_sim_time=6, same shape as d001).
    - d003, created strictly after every write anchor a fixture reference
      cell can produce (`joined_sim_time=10` on the membership row,
      `last_mutation_sim_time=10` on the untracked `prop__doctor_id`) yet
      <= slice_at=100 (created_sim_time=50) — so Phase 2's
      `created_after_reference`-constrained donor pool is non-empty too.

    license_number/notes are untracked like before -- no series backs them
    for any doctor row.
    """
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d001",  # record_id
            5,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            5,  # last_mutation_sim_time
            0,  # record_index
            "Dr. Smith",  # prop__name
            "48213",  # prop__license_number
            "café",  # prop__notes
            "cardiology",  # prop__specialty
        ],
    )
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d002",  # record_id -- ordinary donor
            6,  # created_sim_time
            True,  # active
            # deactivated_at = NULL
            6,  # last_mutation_sim_time
            1,  # record_index
            "Dr. Jones",  # prop__name
            "77104",  # prop__license_number
            "clinic",  # prop__notes
            "radiology",  # prop__specialty
        ],
    )
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",  # fork_path
            "d003",  # record_id -- late-created donor
            50,  # created_sim_time -- after every write anchor, <= slice_at
            True,  # active
            # deactivated_at = NULL
            50,  # last_mutation_sim_time
            2,  # record_index
            "Dr. Patel",  # prop__name
            "90210",  # prop__license_number
            "annex",  # prop__notes
            "surgery",  # prop__specialty
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
    history_rows = _populate_history(conn) + _populate_history_a002_genesis(conn)
    actor_rows = _populate_records_actor(conn)
    doctor_rows = _populate_records_doctor(conn)
    membership_rows = _populate_membership(conn)
    return history_rows, actor_rows, doctor_rows, membership_rows


# ---------------------------------------------------------------------------
# Per-fixture builders
# ---------------------------------------------------------------------------


def build_spanning(dest: Path) -> None:
    """Build the spanning fixture into dest.

    A single-branch (trunk-only) sanitised emit with no firings table and
    no provenance columns. Exercises: history (6 base cols), records__actor
    (two rows, record_index 0-1) with a references-annotated prop__doctor_id
    paired with ref_index__doctor_id (a001 resolves to d001; a002 is a
    NULL-together pair -- both cells NULL), a closed-domain slice_only
    prop__status, a constant-class sub-type discriminator prop__actor_type,
    records__doctor (three rows, record_index 0-2, record_id shapes mixing a
    hex-valid alnum id (d001), a pure decimal-string id ("1005"), and a
    hex-digest id ("9f2ab1") -- the adversarial id mix an
    id/index-conflating implementation cannot pass by coincidence), and
    membership__actor__appointments with elem__* and member__*__kind/id
    columns, plus pinned_ids, runtime, enum_domains, and record_roles.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    history_rows, actor_rows, doctor_rows, membership_rows = _build_spanning_db(conn)
    conn.close()

    tables = _spanning_tables(history_rows, actor_rows, doctor_rows, membership_rows)
    write_emit(
        dest,
        tables=tables,
        branches=_SPANNING_BRANCHES,
        extra=_base_extra(include_record_roles=True),
    )


def build_history_series(dest: Path) -> None:
    """Build a spanning-shaped emit whose history carries multi-event series.

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
      `mutate_cells @ mojibake`), plus one appended tracked VARCHAR column,
      `prop__specialty` (doctor's sole tracked, genesis-only series) — see
      `_history_series_doctor_columns` for why every other fixture VARCHAR
      value is the wrong shape for either untracked mutation.
    - history carries: at least two distinct series with >= 2 events each,
      one series with >= 4 events (so a random freeze cut has a real
      range), distinct sim_time ticks within each series, at least one
      event with sim_time > slice_at (exercising the C6 pre-slice gate),
      two numeric tracked series (`wait_minutes`, `temperature_c`) backing
      the two numeric prop__ columns above, one genesis-only series per
      doctor backing `prop__specialty`, and every records prop__ cell
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
    extra = _base_extra(include_record_roles=True)
    enum_domains = extra["enum_domains"]
    assert isinstance(enum_domains, dict)
    actor_domains = enum_domains["actor"]
    assert isinstance(actor_domains, dict)
    actor_domains["actor_type"] = ["nurse", "patient"]
    write_emit(dest, tables=tables, branches=_SPANNING_BRANCHES, extra=extra)


def build_membership_intervals(dest: Path) -> None:
    """Build a spanning-shaped emit whose membership carries
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
    extra = _base_extra(include_record_roles=True)
    enum_domains = extra["enum_domains"]
    assert isinstance(enum_domains, dict)
    actor_domains = enum_domains["actor"]
    assert isinstance(actor_domains, dict)
    actor_domains["actor_type"] = ["nurse", "patient"]
    write_emit(dest, tables=tables, branches=_SPANNING_BRANCHES, extra=extra)


def build_wrong_version(dest: Path) -> None:
    """Build the wrong_version fixture into dest.

    An otherwise-valid emit whose base_format_version is not
    SUPPORTED_BASE_FORMAT_VERSION (UNSUPPORTED_VERSION_SENTINEL instead).
    open_emit raises UnsupportedBaseFormatVersionError on this fixture.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    write_emit(
        dest,
        tables=[_table_spec("history", "fixed", _HISTORY_COLUMNS, 0)],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        base_format_version=UNSUPPORTED_VERSION_SENTINEL,
        schema_valid=False,
    )


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

    write_emit(
        dest,
        tables=[_table_spec("history", "fixed", broken_columns, 0)],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 0}],
    )


def build_c5_prop_missing(dest: Path) -> None:
    """Build the c5_prop_missing fixture into dest.

    A prop__* column dropped from the records DuckDB table but still declared
    in the sidecar (itself a well-formed records shape). C2 fails alone: C5's
    removed catalog re-check no longer sees the sidecar/catalog mismatch --
    C2's element-wise catalog<->sidecar agreement is the sole catalog carrier.
    """
    dest.mkdir(parents=True, exist_ok=True)

    db_columns = [c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "prop__name"]
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", db_columns))
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "records__actor",
                "records",
                _RECORDS_ACTOR_COLUMNS,
                0,
                record_kind="actor",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 0}],
    )


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

    write_emit(
        dest,
        tables=[
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
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def build_schema_mismatch(dest: Path) -> None:
    """Build the schema_mismatch fixture into dest.

    A phantom prop__phantom_column is declared in the sidecar (itself a
    well-formed records shape -- an unreferenced trailing prop__ column) but
    absent in the DuckDB records table. C2 fails alone: C5's removed catalog
    re-check no longer sees the sidecar/catalog mismatch.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.close()

    phantom_columns = copy.deepcopy(_RECORDS_ACTOR_COLUMNS)
    phantom_columns.append(
        prop_column(
            "prop__phantom_column",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        )
    )
    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec(
                "records__actor",
                "records",
                phantom_columns,
                0,
                record_kind="actor",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 0}],
    )


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

    write_emit(
        dest,
        tables=[_table_spec("history", "fixed", _HISTORY_COLUMNS, 2)],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def build_refs_dangling(dest: Path) -> None:
    """Build the refs_dangling fixture into dest.

    A records__actor row has a prop__doctor_id that does not exist in any
    records__doctor table (its ref_index__doctor_id sibling is likewise
    dangling -- pair resolution is producer-guaranteed, not C1-C13-verified).
    This is a dangling records-prop reference, which is outside C1–C12 (C10
    resolves only membership references; C11 checks the history_tracked flag,
    not prop reference integrity). validate passes. The tracked prop__name
    column carries its own unconditional genesis row at a001's
    created_sim_time=10 -- required so this fixture fails nothing but the
    dangling-reference boundary it exists to exercise.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))

    history_rows = _populate_history(conn)  # a001's prop__name genesis row

    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a001",
            10,
            True,
            10,  # last_mutation_sim_time
            0,  # record_index
            "Alice",
            "active",
            "nonexistent_doctor_id",  # dangling reference
            -1,  # ref_index__doctor_id -- likewise dangling, never verified
            "patient",
        ],
    )
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, history_rows),
            _table_spec(
                "records__actor",
                "records",
                _RECORDS_ACTOR_COLUMNS,
                1,
                record_kind="actor",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def _c13_records_actor_columns(prop_col: dict[str, object]) -> list[dict[str, object]]:
    """Build a minimal records__actor column list carrying exactly one prop__ column.

    Shared by the C11/C13 negative fixtures below, which each isolate their
    defect to a single flagged prop__ column rather than the spanning
    fixture's four. Carries `record_index` (required on every
    records-category table) so these C11/C13 negatives fail only the check
    they are named for, never C5.
    """
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_col,
    ]


def _c13_populate_records_actor(conn: duckdb.DuckDBPyConnection, value: str) -> int:
    """Insert one records__actor row (a001, created_sim_time=10); return row count."""
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, value],
    )
    return 1


def _c13_populate_history_name_series(
    conn: duckdb.DuckDBPyConnection, rows: list[tuple[int, str | None]]
) -> int:
    """Insert (sim_time, value) rows for the (actor, a001, name) series."""
    for sim_time, value in rows:
        conn.execute(
            "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
            ["trunk", "actor", "a001", "name", sim_time, value],
        )
    return len(rows)


def build_c13_broken_pairing(dest: Path) -> None:
    """Build the c13_broken_pairing fixture into dest.

    prop__name declares history_tracked=True with no paired temporal_class --
    built via prop_column, then mutated (the constructor cannot express the
    defect: it requires both attributes together). A genesis history row keeps
    C11's converse and C13's semantic clause satisfied, so this fails C13's
    structural clause alone -- the vendored schema does not enforce the pairing
    (C1 passes).
    """
    dest.mkdir(parents=True, exist_ok=True)
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    del prop_col["temporal_class"]
    columns = _c13_records_actor_columns(prop_col)

    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", columns))
    history_rows = _c13_populate_history_name_series(conn, [(10, "Alice")])
    _c13_populate_records_actor(conn, "Alice")
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, history_rows),
            _table_spec("records__actor", "records", columns, 1, record_kind="actor"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def build_c13_out_of_enum_class(dest: Path) -> None:
    """Build the c13_out_of_enum_class fixture into dest.

    prop__name declares a temporal_class outside the three-value enum -- built
    via prop_column, then mutated. Fails C13's enum clause and necessarily C1
    (the vendored schema enum-constrains the value); written with
    schema_valid=False since the defect is schema-level. A genesis history row
    keeps C11's converse and C13's semantic clause satisfied, so the
    expectation names exactly C1 and C13.
    """
    dest.mkdir(parents=True, exist_ok=True)
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    prop_col["temporal_class"] = "bogus"
    columns = _c13_records_actor_columns(prop_col)

    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", columns))
    history_rows = _c13_populate_history_name_series(conn, [(10, "Alice")])
    _c13_populate_records_actor(conn, "Alice")
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, history_rows),
            _table_spec("records__actor", "records", columns, 1, record_kind="actor"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        schema_valid=False,
    )


def build_c13_missing_genesis(dest: Path) -> None:
    """Build the c13_missing_genesis fixture into dest.

    prop__name is properly paired and tracked; history carries two later rows
    for (actor, name) but none at a001's own created_sim_time=10. C11's
    converse still sees rows for the pair (passes); C13's semantic clause
    fails alone -- no row matches the genesis (kind, record_id, property,
    created_sim_time) key.
    """
    dest.mkdir(parents=True, exist_ok=True)
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    columns = _c13_records_actor_columns(prop_col)

    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", columns))
    history_rows = _c13_populate_history_name_series(
        conn, [(20, "Alice-v1"), (30, "Alice")]
    )
    _c13_populate_records_actor(conn, "Alice")
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, history_rows),
            _table_spec("records__actor", "records", columns, 1, record_kind="actor"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def build_c11_emptied_series(dest: Path) -> None:
    """Build the c11_emptied_series fixture into dest.

    prop__name is properly paired and tracked; records__actor has a row but
    history carries zero rows for (actor, name). C11's converse fails (zero
    rows on a kind with records rows); C13's genesis clause fails too -- zero
    rows implies no genesis row.
    """
    dest.mkdir(parents=True, exist_ok=True)
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    columns = _c13_records_actor_columns(prop_col)

    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", columns))
    _c13_populate_records_actor(conn, "Alice")
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec("records__actor", "records", columns, 1, record_kind="actor"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


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
    extra = _base_extra(include_record_roles=True)
    # Remove 'doctor' from record_roles so C12 fails
    record_roles = copy.deepcopy(_RECORD_ROLES)
    del record_roles["doctor"]
    extra["record_roles"] = record_roles
    write_emit(dest, tables=tables, branches=_SPANNING_BRANCHES, extra=extra)


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
    extra = _base_extra(include_record_roles=True)
    # Remove 'patient' sub-type from actor so C12 fails
    record_roles: dict[str, object] = {
        "actor": {"nurse": "fact"},  # 'patient' omitted — data has 'patient'
        "doctor": "dimension",
    }
    extra["record_roles"] = record_roles
    write_emit(dest, tables=tables, branches=_SPANNING_BRANCHES, extra=extra)


# ---------------------------------------------------------------------------
# C5 shape negatives — each isolates one clause of the amended
# _check_c5_table positional check to records__actor alone.
# ---------------------------------------------------------------------------


def _write_c5_negative(dest: Path, columns: list[dict[str, object]]) -> None:
    """Shared harness for the C5 shape negatives below.

    Each negative supplies a deliberately mis-shaped records__actor column
    list; the DuckDB catalog carries the identical (broken) shape so C2 stays
    silent and only C5's positional check can fail. Zero rows -- the defect
    is purely structural, so no row data is needed. write_emit's own
    records-shape assertion is the same net C5 enforces at read time, so it
    is opted out here via records_shape_valid=False -- the deliberate defect
    this harness exists to write.

    Args:
        dest: The emit directory.
        columns: The (broken) records__actor column list, used for both the
            DuckDB catalog and the sidecar.
    """
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", columns))
    conn.close()

    write_emit(
        dest,
        tables=[
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 0),
            _table_spec("records__actor", "records", columns, 0, record_kind="actor"),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        records_shape_valid=False,
    )


def build_c5_missing_record_index(dest: Path) -> None:
    """Build the c5_missing_record_index fixture into dest.

    record_index is dropped entirely from records__actor; the property block
    starts one slot early. C5 fails alone.
    """
    columns = [c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "record_index"]
    _write_c5_negative(dest, columns)


def build_c5_misplaced_record_index(dest: Path) -> None:
    """Build the c5_misplaced_record_index fixture into dest.

    record_index is moved to the end of the column list instead of sitting
    immediately after the lifecycle prefix. C5 fails alone.
    """
    columns = [c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "record_index"]
    record_index_col = next(
        c for c in _RECORDS_ACTOR_COLUMNS if c["name"] == "record_index"
    )
    columns.append(record_index_col)
    _write_c5_negative(dest, columns)


def build_c5_prop_without_ref_index(dest: Path) -> None:
    """Build the c5_prop_without_ref_index fixture into dest.

    The reference-annotated prop__doctor_id column's ref_index__doctor_id
    sibling is dropped; prop__doctor_id's own `references` annotation is
    unchanged. C5 fails alone.
    """
    columns = [c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "ref_index__doctor_id"]
    _write_c5_negative(dest, columns)


def build_c5_ref_index_without_reference(dest: Path) -> None:
    """Build the c5_ref_index_without_reference fixture into dest.

    A ref_index__ column is appended after a non-reference-annotated prop__
    column (prop__actor_type carries no `references`), so it has no
    reference-annotated predecessor to pair with. C5 fails alone.
    """
    columns = copy.deepcopy(_RECORDS_ACTOR_COLUMNS)
    columns.append(identity_column("ref_index__actor_type", "BIGINT"))
    _write_c5_negative(dest, columns)


def build_c5_ref_index_wrong_type(dest: Path) -> None:
    """Build the c5_ref_index_wrong_type fixture into dest.

    ref_index__doctor_id is declared VARCHAR instead of the pinned BIGINT.
    C5 fails alone.
    """
    columns = copy.deepcopy(_RECORDS_ACTOR_COLUMNS)
    for col in columns:
        if col["name"] == "ref_index__doctor_id":
            col["type"] = "VARCHAR"
    _write_c5_negative(dest, columns)


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
    "c13_broken_pairing": build_c13_broken_pairing,
    "c13_out_of_enum_class": build_c13_out_of_enum_class,
    "c13_missing_genesis": build_c13_missing_genesis,
    "c11_emptied_series": build_c11_emptied_series,
    "c5_missing_record_index": build_c5_missing_record_index,
    "c5_misplaced_record_index": build_c5_misplaced_record_index,
    "c5_prop_without_ref_index": build_c5_prop_without_ref_index,
    "c5_ref_index_without_reference": build_c5_ref_index_without_reference,
    "c5_ref_index_wrong_type": build_c5_ref_index_wrong_type,
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
