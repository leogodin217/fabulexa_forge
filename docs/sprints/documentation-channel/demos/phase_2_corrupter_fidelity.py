#!/usr/bin/env python
"""
Demo: The corrupter's base-emit writer forwards the seven optional
per-column documentation attributes -- description, unit, min, max,
immutable, required, extra_data -- and the table-level description, under
the writer's existing round-trip rule (join by post-drift name, never
re-looked-up from the source sidecar).

Sprint: documentation-channel
Phase: 2

Builds a documented fixture (a `records__actor` table with a described
prop__status, prop__note, and prop__balance column, plus a table
description), simulates the working set a `schema_drift` operation with a
rename + drop + retype produces -- the way
`corrupters.validate._apply_drift_to_spec` folds one in, via
`dataclasses.replace` -- and writes it out through
`corrupters.base_writer.write_base_emit`. Reopens the written base.json and
prints: the renamed column carrying its original description under its new
name, the dropped column absent, the retyped column's description and unit
intact under its new type, and the table description forwarded.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pyarrow as pa

from fabulexa_forge.corrupters.base_writer import write_base_emit
from fabulexa_forge.corrupters.state import CorruptState, WorkingTable
from fabulexa_forge.reader.sidecar import ColumnSpec, TableSpec

# The source table before the corrupter's schema_drift operation runs --
# fully documented: a table description, and three prop__ columns each
# carrying description (+ unit on the numeric one).
_PRE_DRIFT_SPEC = TableSpec(
    name="records__actor",
    category="records",
    record_kind="actor",
    property=None,
    description="Actors participating in the scenario.",
    rows=1,
    columns=(
        ColumnSpec("fork_path", "VARCHAR", None, None, None),
        ColumnSpec("record_id", "VARCHAR", None, None, None),
        ColumnSpec("created_sim_time", "BIGINT", None, None, None),
        ColumnSpec("active", "BOOLEAN", None, None, None),
        ColumnSpec("deactivated_at", "BIGINT", None, None, None),
        ColumnSpec("last_mutation_sim_time", "BIGINT", None, None, None),
        ColumnSpec("record_index", "BIGINT", None, None, None),
        ColumnSpec(
            "prop__status",
            "VARCHAR",
            None,
            False,
            "slice_only",
            description="The actor's lifecycle status.",
        ),
        ColumnSpec(
            "prop__note",
            "VARCHAR",
            None,
            False,
            "slice_only",
            description="A free-text annotation.",
        ),
        ColumnSpec(
            "prop__balance",
            "DOUBLE",
            None,
            False,
            "slice_only",
            description="The actor's running balance.",
            unit="USD",
        ),
    ),
)

_SOURCE_SIDECAR: dict[str, object] = {
    "base_format_version": 1,
    "surface": "published",
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
    "record_roles": {"actor": "fact"},
    "tables": [],  # write_base_emit regenerates this from the working set
}


def _drifted_working_table() -> WorkingTable:
    """The working set after a `schema_drift` rename + drop + retype --
    prop__status -> prop__account_status, prop__note dropped, prop__balance
    retyped DOUBLE -> VARCHAR -- built the way
    `_apply_drift_to_spec` folds one in: `dataclasses.replace` on each
    surviving column, the dropped column simply absent.
    """
    evolved_columns = tuple(
        replace(col, name="prop__account_status")
        if col.name == "prop__status"
        else replace(col, type="VARCHAR")
        if col.name == "prop__balance"
        else col
        for col in _PRE_DRIFT_SPEC.columns
        if col.name != "prop__note"
    )
    evolved_spec = replace(_PRE_DRIFT_SPEC, columns=evolved_columns)
    data = pa.table(
        {
            "fork_path": pa.array(["trunk"], type=pa.string()),
            "record_id": pa.array(["a001"], type=pa.string()),
            "created_sim_time": pa.array([0], type=pa.int64()),
            "active": pa.array([True], type=pa.bool_()),
            "deactivated_at": pa.array([None], type=pa.int64()),
            "last_mutation_sim_time": pa.array([0], type=pa.int64()),
            "record_index": pa.array([0], type=pa.int64()),
            "prop__account_status": pa.array(["active"], type=pa.string()),
            "prop__balance": pa.array(["12.50"], type=pa.string()),
        }
    )
    return WorkingTable(spec=evolved_spec, data=data)


def main() -> int:
    state = CorruptState(tables={"records__actor": _drifted_working_table()})

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        write_base_emit(state, _SOURCE_SIDECAR, out_dir)

        sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))

    (table,) = sidecar["tables"]
    columns_by_name = {c["name"]: c for c in table["columns"]}

    print("=== Table description forwarded ===")
    print(table["description"])

    print("\n=== Renamed column carries its original description ===")
    print("prop__status" in columns_by_name, "-- old name, should be False")
    account_status = columns_by_name["prop__account_status"]
    print(f"prop__account_status.description = {account_status['description']!r}")

    print("\n=== Dropped column is absent ===")
    print("prop__note" in columns_by_name, "-- should be False")

    print("\n=== Retyped column keeps its description and unit ===")
    balance = columns_by_name["prop__balance"]
    print(
        f"prop__balance.type={balance['type']!r} "
        f"description={balance['description']!r} unit={balance['unit']!r}"
    )

    if table.get("description") != "Actors participating in the scenario.":
        return 1
    if "prop__status" in columns_by_name:
        return 1
    if account_status["description"] != "The actor's lifecycle status.":
        return 1
    if "prop__note" in columns_by_name:
        return 1
    if balance["type"] != "VARCHAR" or balance["unit"] != "USD":
        return 1

    print(
        "\nSUCCESS: rename/drop/retype all forward documentation attributes"
        " through write_base_emit correctly"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
