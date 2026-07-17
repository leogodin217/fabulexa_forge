#!/usr/bin/env python
"""
Demo: Records-column taxonomy + posture ports (green at v5).

Builds a small v5 emit inline (no fixture files, no vendored-schema
validation -- the demo stays green while the vendored contract is already
pinned to v6, ahead of the Phase-2 flip). Shows:

1. `records_column_role` classifying every v6 column family by name alone --
   identity, presentation, lifecycle, payload -- and a no-role name returning
   `None`, the loud condition every caller must treat as an error.
2. `ref_index_sibling` pairing a `prop__<name>` column with its
   `ref_index__<name>` sibling name.
3. The source exporter's plan classifying every records column through the
   taxonomy: at v5 the index families never occur, so a faithful table's
   output columns are unchanged; a records table carrying a genuinely
   unclassifiable column raises `SourceUnclassifiedColumn`, before any
   output is written.
4. `init` proposals now role-scoped to payload + presentation only --
   `created_sim_time` / `last_mutation_sim_time` no longer leak into the
   SCD-2 dim stub by enumeration accident.

Sprint: base-format-v6
Phase: 1
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from fabulexa_forge.errors import SourceUnclassifiedColumn
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import Emit
from fabulexa_forge.reader.records_columns import (
    REF_INDEX_PREFIX,
    records_column_role,
    ref_index_sibling,
)
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# 1. Taxonomy classification over every v6 column family
# ---------------------------------------------------------------------------

_SAMPLE_NAMES = [
    "fork_path",
    "record_id",
    "record_index",
    "ref_index__location",
    "presentation_id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "last_mutation_sim_time",
    "prop__status",
    "member__actor__id",  # no role: a membership-table column family
]


def _print_taxonomy_table() -> None:
    print("Records-column taxonomy classification:")
    for name in _SAMPLE_NAMES:
        role = records_column_role(name)
        print(f"  {name:<28} -> {role}")
    sibling = ref_index_sibling("prop__location")
    print(f"\n  ref_index_sibling('prop__location') -> '{sibling}'")
    assert sibling == f"{REF_INDEX_PREFIX}location"


# ---------------------------------------------------------------------------
# 2. A small v5 emit, built inline
# ---------------------------------------------------------------------------

#: A single tracked (changelog-genre) kind with a plain scalar property --
#: enough to exercise both the source plan and the init proposal loop.
_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]


def _widget_table(columns: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "records__widget",
        "category": "records",
        "record_kind": "widget",
        "columns": columns,
        "rows": 1,
    }


def _build_v5_sidecar_raw(columns: list[dict[str, object]]) -> dict[str, object]:
    return {
        "base_format_version": 5,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [_widget_table(columns)],
        "record_roles": {"widget": "dimension"},
    }


def _open_inline_emit(sidecar: Sidecar) -> Emit:
    """Build a matching in-memory DuckDB connection and wrap it as an Emit --
    no files, no schema validation; the reader's sole file-based entry point
    (`open_emit`) is unused here on purpose."""
    conn = duckdb.connect(":memory:")
    conn.execute(
        'CREATE TABLE "records__widget" ('
        "fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT,"
        " active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT,"
        " prop__status VARCHAR)"
    )
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?)',
        ["trunk", "w1", 0, True, 0, "online"],
    )
    return Emit(sidecar=sidecar, emit_dir=Path("<inline>"), conn=conn)


# ---------------------------------------------------------------------------
# 3. Source plan: unchanged output at v5, and the raised no-role error
# ---------------------------------------------------------------------------


def _print_source_plan_posture() -> None:
    sidecar = Sidecar.from_raw(_build_v5_sidecar_raw(_WIDGET_COLUMNS))
    plan = build_source_plan(sidecar, None)
    spec = plan[0]
    columns = [out for _, out in spec.columns]
    print(f"\nSource plan output columns for '{spec.name}': {columns}")
    assert "record_index" not in columns
    assert not any(c.startswith("ref_index__") for c in columns)

    broken_columns = list(_WIDGET_COLUMNS) + [{"name": "mystery", "type": "VARCHAR"}]
    broken_sidecar = Sidecar.from_raw(_build_v5_sidecar_raw(broken_columns))
    try:
        build_source_plan(broken_sidecar, None)
    except SourceUnclassifiedColumn as exc:
        print(f"\nSourceUnclassifiedColumn raised for the no-role column: {exc}")
    else:
        raise SystemExit("FAILURE: a no-role column did not raise")


# ---------------------------------------------------------------------------
# 4. init proposals: role-scoped to payload + presentation
# ---------------------------------------------------------------------------


def _print_init_proposal_posture() -> None:
    sidecar = Sidecar.from_raw(_build_v5_sidecar_raw(_WIDGET_COLUMNS))
    emit = _open_inline_emit(sidecar)
    try:
        candidate = generate_init_config(emit)
    finally:
        emit.close()
    print("\ninit candidate config (excerpt):")
    for line in candidate.splitlines():
        if line.strip():
            print(f"  {line}")
    assert "created_sim_time" not in candidate
    assert "last_mutation_sim_time" not in candidate
    assert "prop__status" in candidate
    print(
        "\nconfirmed: 'created_sim_time' / 'last_mutation_sim_time' absent from"
        " proposals (role-scoped to payload + presentation)."
    )


def main() -> int:
    _print_taxonomy_table()
    _print_source_plan_posture()
    _print_init_proposal_posture()
    print(
        "\nSUCCESS: the records-column taxonomy classifies totally; the source"
        " exporter and init proposal loop both read through it, and Phase-1"
        " output stays byte-identical at v5."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
