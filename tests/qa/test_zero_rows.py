"""Tests for the zero-row tripwire gate, `tools/qa/zero_rows.py`.

The gate is a data-only script (duckdb + yaml + stdlib; never imports
`fabulexa_forge`), so it is exercised the way the harness runs it: as a
subprocess, judged on its exit code and its JSON report.

Covers:
- every table populated -> exit 0
- one empty table -> exit 1, named under `zero_row_tables`
- the empty table allowlisted for this mode -> exit 0, named under
  `expected_empty_tables`; an allowlist for another mode does not apply
- a stale allowlist entry (table now populated, or absent) -> warning, exit 0
- a dataset with no tables at all -> exit 1
- a malformed allowlist (missing reason) -> exit 3 (ungated)
- an unopenable dataset -> exit 3 (ungated)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb

_GATE = Path(__file__).resolve().parent.parent.parent / "tools" / "qa" / "zero_rows.py"


def _build(path: Path, tables: dict[str, int]) -> Path:
    con = duckdb.connect(str(path))
    try:
        for name, rows in tables.items():
            con.execute(f'CREATE TABLE "{name}" (id BIGINT)')
            if rows:
                con.execute(f'INSERT INTO "{name}" SELECT * FROM range({rows})')
    finally:
        con.close()
    return path


def _config(path: Path, mode: str = "dimensional") -> Path:
    path.write_text(f"mode: {mode}\n")
    return path


def _run(*args: str | Path) -> tuple[int, dict[str, object]]:
    proc = subprocess.run(
        [sys.executable, str(_GATE), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_all_tables_populated_passes(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"dim_a": 3, "fact_b": 5})
    code, report = _run(_config(tmp_path / "c.yaml"), ds)
    assert code == 0
    assert report["pass"] is True
    assert report["zero_row_tables"] == []
    assert report["row_counts"] == {"dim_a": 3, "fact_b": 5}


def test_empty_table_fails_and_is_named(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"dim_a": 3, "fact_leg_crew": 0})
    code, report = _run(_config(tmp_path / "c.yaml"), ds)
    assert code == 1
    assert report["pass"] is False
    assert report["zero_row_tables"] == ["fact_leg_crew"]


def test_allowlisted_empty_table_passes(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"dim_a": 3, "fact_refund": 0})
    allow = tmp_path / "expected-empty.yaml"
    allow.write_text("dimensional:\n  fact_refund: no refunds fire in this horizon\n")
    code, report = _run(_config(tmp_path / "c.yaml"), ds, "--expected-empty", allow)
    assert code == 0
    assert report["expected_empty_tables"] == ["fact_refund"]
    assert report["zero_row_tables"] == []
    assert report["warnings"] == []


def test_allowlist_is_per_mode(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"fact_refund": 0})
    allow = tmp_path / "expected-empty.yaml"
    allow.write_text("source:\n  fact_refund: wrong section\n")
    code, report = _run(_config(tmp_path / "c.yaml"), ds, "--expected-empty", allow)
    assert code == 1
    assert report["zero_row_tables"] == ["fact_refund"]


def test_stale_allowlist_entries_warn_without_failing(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"dim_a": 3})
    allow = tmp_path / "expected-empty.yaml"
    allow.write_text("dimensional:\n  dim_a: once empty\n  gone: never exported\n")
    code, report = _run(_config(tmp_path / "c.yaml"), ds, "--expected-empty", allow)
    assert code == 0
    warnings = report["warnings"]
    assert isinstance(warnings, list) and len(warnings) == 2
    assert any("'dim_a' has 3 rows" in w for w in warnings)
    assert any("'gone' is not in the dataset" in w for w in warnings)


def test_dataset_with_no_tables_fails(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {})
    code, report = _run(_config(tmp_path / "c.yaml"), ds)
    assert code == 1
    assert report["tables"] == 0
    assert "no tables" in str(report["note"])


def test_malformed_allowlist_is_ungated(tmp_path: Path) -> None:
    ds = _build(tmp_path / "ds.duckdb", {"dim_a": 3})
    allow = tmp_path / "expected-empty.yaml"
    allow.write_text("dimensional:\n  fact_refund:\n")  # no reason
    code, report = _run(_config(tmp_path / "c.yaml"), ds, "--expected-empty", allow)
    assert code == 3
    assert report["gated"] is False
    assert "malformed" in str(report["reason"])


def test_unopenable_dataset_is_ungated(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-db.duckdb"
    bogus.write_bytes(b"this is not a duckdb file")
    code, report = _run(_config(tmp_path / "c.yaml"), bogus)
    assert code == 3
    assert report["gated"] is False
