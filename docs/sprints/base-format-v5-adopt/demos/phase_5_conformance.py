#!/usr/bin/env python
"""
Demo: Conformance judges v5 -- C11 converse, C13, and the emptied-series impact.

Four self-contained scenarios:

1. The Phase-3 spanning fixture (tests/reader/_fixtures_build.build_spanning) is
   C1 through C13 conformant -- C11's converse and C13's structural + semantic
   clauses all pass on a genuine v5 emit.
2. A column carrying history_tracked with no paired temporal_class (the
   vendored schema does not enforce the pairing) fails C13's structural clause
   alone.
3. A tracked column whose history carries only a later tick -- no row at the
   record's own created_sim_time -- fails C13's semantic (genesis) clause
   alone; C11's converse still sees rows for the pair.
4. Corrupting the spanning fixture's sole history row (the `(actor, name)`
   series' only tick, also its genesis row) with `drop_events` empties that
   pair entirely: the emitted defect declares `impact: ["C11"]` alone, and
   re-validating the corrupted output fails both C11 (converse) and C13
   (genesis -- zero rows implies no genesis row).

Sprint: base-format-v5-adopt
Phase: 5
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# tests/reader is test infrastructure, not part of the installed package. Put
# tests/ on sys.path so this standalone demo can reuse the Phase-3 fixture
# builder the phase's success criteria refer to by name.
_TESTS_DIR = Path(__file__).resolve().parents[4] / "tests"
sys.path.insert(0, str(_TESTS_DIR))

import duckdb  # noqa: E402
import yaml  # noqa: E402
from _support.sidecar_builder import prop_column, write_emit  # noqa: E402
from reader._fixtures_build import build_spanning  # noqa: E402

from fabulexa_forge.config.models import CorruptConfig  # noqa: E402
from fabulexa_forge.corrupters.engine import corrupt_emit  # noqa: E402
from fabulexa_forge.reader.conformance import validate  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_RECORDS_PREFIX: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]

_CORRUPT_CONFIG_YAML = """
seed: 11
operations:
  - kind: drop_events
    name: empty_name_series
    target:
      category: fixed
      where: { property: "name" }
    amount: { rate: 1.0 }
"""


def _create_table(
    conn: "duckdb.DuckDBPyConnection", name: str, columns: list[dict[str, object]]
) -> None:
    """CREATE TABLE `name` from a sidecar-shaped column list (name + type only)."""
    fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    conn.execute(f"CREATE TABLE {name} ({fragments})")


def _build_single_prop_emit(
    dest: Path,
    prop_col: dict[str, object],
    prop_value: object,
    history_rows: list[tuple[str, str, str, str, int, str]],
) -> None:
    """A minimal single-branch emit: one records__actor row (a001,
    created_sim_time=10) carrying `prop_col`, plus a `history` table seeded
    with `history_rows` -- the shared shape scenarios 2 and 3 each mutate one
    way.
    """
    records_columns = [*_RECORDS_PREFIX, prop_col]
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    _create_table(conn, "history", _HISTORY_COLUMNS)
    _create_table(conn, "records__actor", records_columns)
    for row in history_rows:
        conn.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)", list(row))
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?)",
        ["trunk", "a001", 10, True, 10, prop_value],
    )
    conn.close()

    write_emit(
        dest,
        tables=[
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(history_rows),
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": records_columns,
                "rows": 1,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )


def _assert_failing_checks(emit_dir: Path, expected: set[str], label: str) -> None:
    """Open `emit_dir` and assert exactly `expected` conformance checks fail."""
    with open_emit(emit_dir) as emit:
        report = validate(emit)
    failing = {r.check for r in report.results if not r.passed}
    if failing != expected:
        raise SystemExit(
            f"FAILURE ({label}): expected failing={expected}, got {failing}"
        )
    print(f"{label}: failing checks = {sorted(failing) or '(none)'}")


def _demo_spanning_all_pass(tmp: Path) -> None:
    """The Phase-3 spanning fixture is C1 through C13 conformant."""
    source_dir = tmp / "spanning"
    build_spanning(source_dir)
    with open_emit(source_dir) as emit:
        report = validate(emit)
    seen_ids = tuple(r.check for r in report.results)
    if seen_ids != tuple(f"C{i}" for i in range(1, 14)):
        raise SystemExit(f"FAILURE: registry order is {seen_ids}, expected C1..C13")
    if not report.ok:
        failing = [r.check for r in report.results if not r.passed]
        raise SystemExit(
            f"FAILURE: spanning fixture should be fully conformant: {failing}"
        )
    print("spanning fixture: C1..C13 all pass")


def _demo_broken_pairing(tmp: Path) -> None:
    """history_tracked with no paired temporal_class fails C13's structural
    clause alone -- the vendored schema does not enforce the pairing."""
    dest = tmp / "broken_pairing"
    prop_col = prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    del prop_col["temporal_class"]
    _build_single_prop_emit(
        dest, prop_col, "active", [("trunk", "actor", "a001", "status", 10, "active")]
    )
    _assert_failing_checks(dest, {"C13"}, "broken pairing")


def _demo_missing_genesis(tmp: Path) -> None:
    """A tracked column whose only history tick is later than created_sim_time
    fails C13's genesis clause alone; C11's converse still sees rows."""
    dest = tmp / "missing_genesis"
    prop_col = prop_column(
        "prop__wait_minutes", "BIGINT", history_tracked=True, temporal_class="tracked"
    )
    _build_single_prop_emit(
        dest, prop_col, 7, [("trunk", "actor", "a001", "wait_minutes", 50, "7")]
    )
    _assert_failing_checks(dest, {"C13"}, "missing genesis")


def _demo_emptied_series(tmp: Path) -> None:
    """Dropping the spanning fixture's sole `name` history row empties the
    `(actor, name)` pair: the defect declares impact ["C11"] alone, and the
    corrupted output fails C11 (and C13's genesis clause) on re-validation."""
    source_dir = tmp / "spanning_for_drop"
    out_dir = tmp / "dropped"
    build_spanning(source_dir)
    config = CorruptConfig.model_validate(yaml.safe_load(_CORRUPT_CONFIG_YAML))
    with open_emit(source_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    defects = manifest["defects"]
    if not defects:
        raise SystemExit("FAILURE: expected drop_events to emit at least one defect")
    impacts = {tuple(d["impact"]) for d in defects}
    if impacts != {("C11",)}:
        raise SystemExit(
            f"FAILURE: expected every defect's impact == ('C11',), got {impacts}"
        )
    print(f"emptied series: {len(defects)} defect(s), impact = ('C11',)")

    _assert_failing_checks(out_dir, {"C11", "C13"}, "corrupted output (post-drop)")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _demo_spanning_all_pass(tmp_path)
        _demo_broken_pairing(tmp_path)
        _demo_missing_genesis(tmp_path)
        _demo_emptied_series(tmp_path)
    print(
        "SUCCESS: C11's converse, C13, and drop_events' emptied-series clause "
        "all judge v5 emits correctly."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
