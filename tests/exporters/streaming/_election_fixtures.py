"""Emit construction helper for streaming message-key election tests.

Builds one combined DuckDB-backed emit spanning every population shape the
election render sites (key map, after-image identity, reference/member-field
translation, gates) need to be exercised against:

  - widget: flat, carries `presentation_id` (registry-eligible). w1 is
      created then updated (a 'u' event); w2 is created then deactivated
      (a 'd' event) — the streamed 'u'/'d' pair the election demo and tests
      key off of.
  - gadget: flat, `prop__target_id` references widget — the reference-edge
      translation target.
  - creature: sub-typed (cat/dog via `enum_domains`), carries
      `presentation_id` — the identity-uniformity / union-safety gate
      target.
  - trainer: flat, `prop__pet_id` references creature — the edge
      union-safety gate target (admits creature's full declared domain).
  - person: flat, carries `presentation_id` — a membership stream's owner.
  - pet: flat, carries `presentation_id` — a membership member-field
      reference target.
  - membership__person__waiters: person's membership table, one closed
      interval (join + leave), with a scalar `elem__priority` field and a
      reference `member__companion__kind`/`__id` field pointing at pet.

`presentation_keys` is caller-supplied (test-owned, mirroring
`tests/exporters/base/_base_fixtures.py`) so each test declares exactly the
registry entries its scenario needs.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

DAY_NS = 86_400 * 1_000_000_000  # one civil day, in sim-time nanoseconds

_WIDGET_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_GADGET_COLS: list[dict[str, object]] = [
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
        references="widget",
    ),
    identity_column("ref_index__target_id", "BIGINT"),
]

_CREATURE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__creature_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
]

_TRAINER_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__pet_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="creature",
    ),
    identity_column("ref_index__pet_id", "BIGINT"),
]

_PERSON_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_PET_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_WAITERS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__priority", "type": "VARCHAR"},
    {"name": "member__companion__kind", "type": "VARCHAR"},
    {"name": "member__companion__id", "type": "VARCHAR"},
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

#: cat/dog share one empty-prefix counter key space — declared for every
#: gate test (identity-uniformity, identity union-safety, edge union-safety)
#: so the presentation-key registry lookup itself never fails first.
CREATURE_UNSAFE_REGISTRY: dict[str, object] = {
    "creature": {
        "sub_types": {
            "cat": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
            "dog": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
        },
        # unique_within omitted: both sub-types share an empty counter
        # prefix and are not pairwise union-safe.
        "branch_stable": False,
        "slice_stable": False,
    },
}


def _flat_presentation_key(prefix: str) -> dict[str, object]:
    """One flat-kind whole-column presentation_keys claim."""
    return {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": prefix, "width": 3},
        }
    }


#: The full registry every non-gate election test uses: widget/person/pet
#: flat claims plus the creature gate registry above. `gadget` and `trainer`
#: are deliberately absent — they carry no presentation_id claim of their
#: own (the ElectionPresentationUndeclared resolution-error test targets
#: `gadget` on this exact registry).
FULL_REGISTRY: dict[str, object] = {
    "widget": _flat_presentation_key("W_"),
    "person": _flat_presentation_key("P_"),
    "pet": _flat_presentation_key("PET_"),
    **CREATURE_UNSAFE_REGISTRY,
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def build_election_emit(
    tmp_path: Path,
    *,
    presentation_keys: dict[str, object] | None = None,
    duplicate_widget_presentation_id: bool = False,
) -> Path:
    """Build the combined widget/gadget/creature/trainer/person/pet emit.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        presentation_keys: The sidecar `presentation_keys` block; omitted
            (the default) leaves every population registry-undeclared.
        duplicate_widget_presentation_id: When True, adds a third widget
            record ("w1b") sharing w1's "W_001" presentation_id — the
            elected-key uniqueness guard's target
            (`ElectedKeyDuplicate`).

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(_ddl("records__widget", _WIDGET_COLS))
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", "W_001", 0, True, 100, 0, "active"],
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "w2", "W_002", 0, False, 200, 200, 1, "waiting"],
    )
    widget_rows = 2
    if duplicate_widget_presentation_id:
        conn.execute(
            'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
            ["trunk", "w1b", "W_001", 0, True, 0, 2, "new"],
        )
        widget_rows = 3

    conn.execute(_ddl("history", _HISTORY_COLS))
    history_rows = [
        ("trunk", "widget", "w1", "status", 0, "new"),
        ("trunk", "widget", "w1", "status", 100, "active"),
        ("trunk", "widget", "w2", "status", 0, "waiting"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))

    conn.execute(_ddl("records__gadget", _GADGET_COLS))
    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)',
        ["trunk", "g1", 0, True, 0, 0, "w1"],
    )
    conn.execute(
        'INSERT INTO "records__gadget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)',
        ["trunk", "g2", 0, True, 0, 1, "w2"],
    )

    conn.execute(_ddl("records__creature", _CREATURE_COLS))
    conn.execute(
        'INSERT INTO "records__creature" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "c_cat1", "C1", 0, True, 0, 0, "cat"],
    )
    conn.execute(
        'INSERT INTO "records__creature" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "c_dog1", "D1", 0, True, 0, 1, "dog"],
    )

    conn.execute(_ddl("records__trainer", _TRAINER_COLS))
    conn.execute(
        'INSERT INTO "records__trainer" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)',
        ["trunk", "t1", 0, True, 0, 0, "c_cat1"],
    )

    conn.execute(_ddl("records__person", _PERSON_COLS))
    conn.execute(
        'INSERT INTO "records__person" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "p1", "P_001", 0, True, 0, 0],
    )

    conn.execute(_ddl("records__pet", _PET_COLS))
    conn.execute(
        'INSERT INTO "records__pet" VALUES (?, ?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "q1", "PET_001", 0, True, 0, 0],
    )

    conn.execute(_ddl("membership__person__waiters", _WAITERS_COLS))
    conn.execute(
        'INSERT INTO "membership__person__waiters" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p1", 0, 300, "high", "pet", "q1"],
    )

    conn.close()

    extra: dict[str, object] = {
        "enum_domains": {"creature": {"creature_type": ["cat", "dog"]}},
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
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": _WIDGET_COLS,
                "rows": widget_rows,
            },
            {
                "name": "records__gadget",
                "category": "records",
                "record_kind": "gadget",
                "columns": _GADGET_COLS,
                "rows": 2,
            },
            {
                "name": "records__creature",
                "category": "records",
                "record_kind": "creature",
                "columns": _CREATURE_COLS,
                "rows": 2,
            },
            {
                "name": "records__trainer",
                "category": "records",
                "record_kind": "trainer",
                "columns": _TRAINER_COLS,
                "rows": 1,
            },
            {
                "name": "records__person",
                "category": "records",
                "record_kind": "person",
                "columns": _PERSON_COLS,
                "rows": 1,
            },
            {
                "name": "records__pet",
                "category": "records",
                "record_kind": "pet",
                "columns": _PET_COLS,
                "rows": 1,
            },
            {
                "name": "membership__person__waiters",
                "category": "membership",
                "record_kind": "person",
                "property": "waiters",
                "columns": _WAITERS_COLS,
                "rows": 1,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(history_rows),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 5 * DAY_NS}],
        extra=extra,
    )
    return tmp_path
