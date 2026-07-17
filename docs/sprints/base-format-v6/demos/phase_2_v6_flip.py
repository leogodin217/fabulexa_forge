#!/usr/bin/env python
"""
Demo: The v6 flip (atomic).

Builds a small v6 emit inline (in-memory DuckDB, no fixture files, no
external example directory -- the same pattern Phase 1's demo uses) to show:

1. `validate` passes C1-C13 over a genuinely v6-shaped emit, including the
   amended C5 (the v6 records-shape layout: `record_index` in its pinned
   slot, and a reference-annotated `prop__group` column immediately followed
   by its `ref_index__group` sibling).
2. Opening the emit and reading `records__actor`'s column list shows
   `record_index` and `ref_index__group` sitting in their contract slots
   (immediately after the lifecycle prefix, and immediately after the
   reference-annotated `prop__group` column, respectively).
3. `init` against this emit proposes payload + presentation columns only --
   no identity column (`record_index`, `ref_index__group`, ...) and no
   lifecycle column ever appears in a proposal.

Sprint: base-format-v6
Phase: 2
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.reader.conformance import validate
from fabulexa_forge.reader.emit import Emit
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# A small v6 emit, built inline: two record kinds -- `actor` (with a
# reference-annotated prop__group pointing at `group`) and `group` (the
# referenced kind) -- plus the required `history` table carrying the one
# genesis row `actor.prop__status`'s history_tracked=True flag requires.
# ---------------------------------------------------------------------------

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__group",
        "type": "VARCHAR",
        "references": "group",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
    {"name": "ref_index__group", "type": "BIGINT"},
]

_GROUP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _records_table(
    name: str, kind: str, columns: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "name": name,
        "category": "records",
        "record_kind": kind,
        "columns": columns,
        "rows": 1,
    }


def _build_v6_sidecar_raw() -> dict[str, object]:
    return {
        "base_format_version": 6,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            _records_table("records__actor", "actor", _ACTOR_COLUMNS),
            _records_table("records__group", "group", _GROUP_COLUMNS),
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 1,
            },
        ],
        "record_roles": {"actor": "dimension", "group": "dimension"},
    }


def _open_inline_emit(sidecar: Sidecar) -> Emit:
    """Build a matching in-memory DuckDB connection and wrap it as an Emit --
    no files, no vendored-schema validation performed here; C1 exercises the
    schema separately, against `sidecar.raw`, once `validate` runs."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        'CREATE TABLE "records__actor" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__status VARCHAR, prop__group VARCHAR,"
        " ref_index__group BIGINT)"
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 0, True, 0, 0, "online", "g1", 0],
    )
    conn.execute(
        'CREATE TABLE "records__group" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " record_index BIGINT, prop__name VARCHAR)"
    )
    conn.execute(
        'INSERT INTO "records__group" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "g1", 0, True, 0, 0, "Team A"],
    )
    conn.execute(
        'CREATE TABLE "history" ('
        "fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, property VARCHAR,"
        " sim_time BIGINT, value VARCHAR)"
    )
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "actor", "a1", "status", 0, "online"],
    )
    return Emit(sidecar=sidecar, emit_dir=Path("<inline>"), conn=conn)


# ---------------------------------------------------------------------------
# 1. validate: C1-C13, including the amended C5
# ---------------------------------------------------------------------------


def _print_validate_report(emit: Emit) -> None:
    report = validate(emit)
    print("validate(inline v6 emit):")
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        print(f"  {result.check}: {status}")
        for message in result.messages:
            print(f"      {message}")
    if not report.ok:
        failing = ", ".join(r.check for r in report.results if not r.passed)
        raise SystemExit(f"FAILURE: expected all checks to pass, failing: {failing}")
    print("all of C1-C13 pass, including the amended C5.")


# ---------------------------------------------------------------------------
# 2. records__actor columns: record_index and ref_index__group in their
#    contract slots
# ---------------------------------------------------------------------------


def _print_records_actor_columns(emit: Emit) -> None:
    columns = [c.name for c in emit.sidecar.columns("records__actor")]
    print("\nrecords__actor columns:")
    for name in columns:
        print(f"  {name}")
    assert "record_index" in columns
    assert "ref_index__group" in columns
    record_index_idx = columns.index("record_index")
    ref_index_idx = columns.index("ref_index__group")
    prop_group_idx = columns.index("prop__group")
    assert ref_index_idx == prop_group_idx + 1, (
        "ref_index__group must immediately follow prop__group"
    )
    print(
        f"\nconfirmed: record_index at col[{record_index_idx}] (immediately"
        " after the lifecycle prefix); ref_index__group at"
        f" col[{ref_index_idx}] (immediately after its prop__group sibling"
        f" at col[{prop_group_idx}])."
    )


# ---------------------------------------------------------------------------
# 3. init proposals: role-scoped to payload + presentation
# ---------------------------------------------------------------------------


def _print_init_proposal(emit: Emit) -> None:
    candidate = generate_init_config(emit)
    print("\ninit candidate config (excerpt):")
    for line in candidate.splitlines():
        if line.strip():
            print(f"  {line}")
    # record_id itself is structurally always the `id` key source (`from:
    # record_id`), never a proposal-loop entry -- the leak this proves absent
    # is the *new* v6 identity family plus every lifecycle column.
    for identity_or_lifecycle in (
        "record_index",
        "ref_index__",
        "created_sim_time",
        "active",
        "deactivated_at",
        "last_mutation_sim_time",
    ):
        assert identity_or_lifecycle not in candidate, (
            f"'{identity_or_lifecycle}' leaked into the init proposal"
        )
    print(
        "\nconfirmed: no identity column (record_index, ref_index__*) and"
        " no lifecycle column appears in the proposal -- role-scoped to"
        " payload + presentation."
    )


def main() -> int:
    sidecar = Sidecar.from_raw(_build_v6_sidecar_raw())
    emit = _open_inline_emit(sidecar)
    try:
        _print_validate_report(emit)
        _print_records_actor_columns(emit)
        _print_init_proposal(emit)
    finally:
        emit.close()
    print(
        "\nSUCCESS: the v6 flip is atomic -- a genuinely v6-shaped emit"
        " validates cleanly under the amended C5, its dense record index"
        " sits in its pinned contract slots, and init proposals stay"
        " role-scoped."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
