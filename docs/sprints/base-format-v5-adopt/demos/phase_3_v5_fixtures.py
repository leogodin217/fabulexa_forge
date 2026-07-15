#!/usr/bin/env python
"""
Demo: One sidecar authority; fixtures become v5 emits.

Builds a small records__patient emit through the new `tests/_support`
fixture-support module alone (`prop_column` + `write_emit`):

1. Every prop__ column carries the (history_tracked, temporal_class) pair,
   total coverage, all three classes represented (constant, tracked,
   slice_only).
2. `history` carries a genesis row at the record's `created_sim_time` for
   each history_tracked column -- including a NULL-valued one for a property
   absent at creation.
3. `write_emit(schema_valid=True)` on a table set missing a schema-required
   field fails at construction, naming the field -- never silently writing a
   broken fixture.

Sprint: base-format-v5-adopt
Phase: 3
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# tests/_support is test infrastructure, not part of the installed package. Put
# tests/ on sys.path so this standalone demo can reuse the one fixture-sidecar
# authority the phase's success criteria refer to by name.
_TESTS_DIR = Path(__file__).resolve().parents[4] / "tests"
sys.path.insert(0, str(_TESTS_DIR))

import duckdb  # noqa: E402
from _support.sidecar_builder import prop_column, write_emit  # noqa: E402

from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_TABLE = "records__patient"
_CREATED_SIM_TIME = 0


def _build_emit(emit_dir: Path) -> None:
    """Build a records__patient emit with all three temporal classes."""
    firings_table: dict[str, object] = {
        "name": "firings",
        "category": "fixed",
        "columns": [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "sim_time", "type": "BIGINT"},
        ],
        "rows": 0,
    }
    records_table: dict[str, object] = {
        "name": _TABLE,
        "category": "records",
        "record_kind": "patient",
        "columns": [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "record_id", "type": "VARCHAR"},
            {"name": "created_sim_time", "type": "BIGINT"},
            prop_column(
                "prop__patient_id",
                "VARCHAR",
                history_tracked=True,
                temporal_class="constant",
            ),
            prop_column(
                "prop__status",
                "VARCHAR",
                history_tracked=True,
                temporal_class="tracked",
            ),
            prop_column(
                "prop__insurer",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            ),
        ],
        "rows": 1,
    }
    history_table: dict[str, object] = {
        "name": "history",
        "category": "fixed",
        "columns": [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "kind", "type": "VARCHAR"},
            {"name": "record_id", "type": "VARCHAR"},
            {"name": "property", "type": "VARCHAR"},
            {"name": "sim_time", "type": "BIGINT"},
            {"name": "value", "type": "VARCHAR"},
        ],
        "rows": 2,
    }
    write_emit(
        emit_dir,
        tables=[firings_table, records_table, history_table],
        schema_valid=True,
    )

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute("CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)")
    conn.execute(
        "CREATE TABLE records__patient "
        "(fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT, "
        "prop__patient_id VARCHAR, prop__status VARCHAR, prop__insurer VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE history "
        "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
        "property VARCHAR, sim_time BIGINT, value VARCHAR)"
    )
    conn.execute(
        "INSERT INTO records__patient VALUES ('trunk', 'r001', 0, 'P001', NULL, NULL)"
    )
    # Genesis rows, unconditional for every history_tracked property -- including
    # the NULL-valued one for a property absent at creation.
    conn.execute(
        "INSERT INTO history VALUES ('trunk', 'patient', 'r001', 'patient_id', "
        "0, 'P001')"
    )
    conn.execute(
        "INSERT INTO history VALUES ('trunk', 'patient', 'r001', 'status', 0, NULL)"
    )
    conn.close()


def _print_temporal_class_coverage(emit_dir: Path) -> None:
    """Every prop__ column carries its (history_tracked, temporal_class) pair."""
    with open_emit(emit_dir) as emit:
        seen_classes: set[str] = set()
        for column in emit.sidecar.columns(_TABLE):
            if not column.name.startswith("prop__"):
                continue
            print(
                f"{_TABLE}.{column.name} -> "
                f"history_tracked={column.history_tracked!r}, "
                f"temporal_class={column.temporal_class!r}"
            )
            assert column.temporal_class is not None
            seen_classes.add(column.temporal_class)
    if seen_classes != {"constant", "tracked", "slice_only"}:
        raise SystemExit(
            f"FAILURE: expected all three classes, got {sorted(seen_classes)}"
        )
    print("Total coverage: all three temporal classes represented.")


def _query_genesis_rows(emit_dir: Path) -> None:
    """`history` carries a genesis row at created_sim_time, one NULL-valued."""
    with open_emit(emit_dir) as emit:
        rows = emit.query(
            "SELECT property, sim_time, value FROM history "
            "WHERE record_id = ? AND sim_time = ? ORDER BY property",
            ("r001", _CREATED_SIM_TIME),
        )
    if len(rows) != 2:
        raise SystemExit(f"FAILURE: expected 2 genesis rows, got {rows}")
    by_property = {row[0]: row[2] for row in rows}
    if by_property["patient_id"] != "P001":
        raise SystemExit(f"FAILURE: patient_id genesis value wrong: {rows}")
    if by_property["status"] is not None:
        raise SystemExit(f"FAILURE: status genesis value should be NULL: {rows}")
    print(
        "Genesis rows at created_sim_time=0: "
        f"patient_id={by_property['patient_id']!r}, "
        f"status={by_property['status']!r} (NULL -- absent at creation)."
    )


def _show_construction_failure_naming_the_field(emit_dir: Path) -> None:
    """write_emit(schema_valid=True) rejects a table missing a required field."""
    broken_table: dict[str, object] = {
        "name": "firings",
        "category": "fixed",
        "columns": [{"name": "fork_path", "type": "VARCHAR"}],
        # 'rows' is schema-required and deliberately omitted.
    }
    try:
        write_emit(emit_dir, tables=[broken_table], schema_valid=True)
    except ValueError as exc:
        if "rows" not in str(exc):
            raise SystemExit(
                f"FAILURE: construction error must name 'rows': {exc}"
            ) from None
        print(f"write_emit(schema_valid=True) refused construction: {exc}")
    else:
        raise SystemExit("FAILURE: missing 'rows' should have failed construction")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_emit(emit_dir)
        _print_temporal_class_coverage(emit_dir)
        _query_genesis_rows(emit_dir)
    with tempfile.TemporaryDirectory() as tmp:
        _show_construction_failure_naming_the_field(Path(tmp))
    print("SUCCESS: tests/_support/sidecar_builder.py is the one sidecar authority.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
