#!/usr/bin/env python
"""
Demo: The decimal canonical family in `compare` — `family_of` maps DECIMAL,
`encode_value` normalizes scale, and the engine's family-coverage validation
admits DECIMAL columns.
Sprint: value-rendering-elections
Phase: 7

Builds small expected/actual DuckDB files with DECIMAL columns and drives
`compare_datasets` end to end:
- a decimal-elected export equals its expected render (identical values,
  identical declared scale)
- the same values at different declared scales still compare equal (scale
  normalization: `1.50` == `1.5`)
- a genuinely differing value reports as a row discrepancy, never an error
"""

import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge.compare import compare_datasets


def _build_duckdb(path: Path, statements: list[str]) -> Path:
    conn = duckdb.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()
    return path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # --- identical values, identical declared scale --------------------
        identical_expected = _build_duckdb(
            root / "identical_expected.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(18,3))",
                "INSERT INTO amounts VALUES (1, 1.50), (2, 42.00)",
            ],
        )
        identical_actual = _build_duckdb(
            root / "identical_actual.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(18,3))",
                "INSERT INTO amounts VALUES (1, 1.50), (2, 42.00)",
            ],
        )
        result = compare_datasets(identical_expected, identical_actual)
        print(f"identical decimal render: equal={result.equal}")
        assert result.equal
        assert result.tables[0].schema == ()

        # --- same values, different declared (precision, scale) ------------
        scaled_expected = _build_duckdb(
            root / "scaled_expected.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(18,3))",
                "INSERT INTO amounts VALUES (1, 1.50)",
            ],
        )
        scaled_actual = _build_duckdb(
            root / "scaled_actual.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(9,1))",
                "INSERT INTO amounts VALUES (1, 1.5)",
            ],
        )
        result = compare_datasets(scaled_expected, scaled_actual)
        print(
            "DECIMAL(18,3) 1.50 vs DECIMAL(9,1) 1.5: "
            f"equal={result.equal} (scale-normalized)"
        )
        assert result.equal
        assert result.tables[0].schema == ()

        # --- genuinely differing value: a row discrepancy, not an error ----
        diff_expected = _build_duckdb(
            root / "diff_expected.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(18,3))",
                "INSERT INTO amounts VALUES (1, 1.50)",
            ],
        )
        diff_actual = _build_duckdb(
            root / "diff_actual.duckdb",
            [
                "CREATE TABLE amounts (id BIGINT, total DECIMAL(18,3))",
                "INSERT INTO amounts VALUES (1, 1.51)",
            ],
        )
        result = compare_datasets(diff_expected, diff_actual)
        table = result.tables[0]
        print(f"1.50 vs 1.51: equal={result.equal}, schema={table.schema}")
        assert not result.equal
        assert table.schema == ()  # DECIMAL is a compatible family, not incompatible
        assert table.rows is not None
        assert table.rows.missing_total == 1
        assert table.rows.extra_total == 1

    print(
        "SUCCESS: compare admits DECIMAL via the decimal canonical family, "
        "scale-normalized"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
