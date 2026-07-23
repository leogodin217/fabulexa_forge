#!/usr/bin/env python3
"""Referential-integrity checker — DATA-ONLY (duckdb + stdlib only).

For every `fact_*` table in the given dataset, every column named `<x>_id`
(except the table's own `id` column) must have every non-null value present
as an `id` value in at least one dimension/fact table in the same dataset.
Reports orphan values + their counts.

This script imports ONLY `duckdb` + Python stdlib. It must never import
`fabulexa_forge` — it verifies exporter *output*, so trusting the exporter's
own code would make the check circular.

Usage:
    refs_resolve.py <dataset.duckdb>
"""

from __future__ import annotations

import argparse
import json

import duckdb


def find_id_bearing_tables(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    """table name -> its column list, for every table that carries an `id` column."""
    tables = [row[0] for row in con.execute("show tables").fetchall()]
    out = {}
    for table in tables:
        cols = [row[0] for row in con.execute(f"describe {table}").fetchall()]
        if "id" in cols:
            out[table] = cols
    return out


def check_dataset(con: duckdb.DuckDBPyConnection) -> dict:
    id_tables = find_id_bearing_tables(con)
    fact_tables = [t for t in id_tables if t.startswith("fact_")]

    results = []
    for table in fact_tables:
        cols = id_tables[table]
        fk_cols = [c for c in cols if c.endswith("_id") and c != "id"]
        for fk_col in fk_cols:
            union_ids = " union all ".join(f"select id from {t}" for t in id_tables)
            orphan_rows = con.execute(
                f"""
                select distinct {fk_col}
                from {table}
                where {fk_col} is not null
                  and {fk_col} not in (select id from ({union_ids}))
                limit 5
                """
            ).fetchall()
            orphan_count = con.execute(
                f"""
                select count(*)
                from {table}
                where {fk_col} is not null
                  and {fk_col} not in (select id from ({union_ids}))
                """
            ).fetchone()[0]
            results.append(
                {
                    "table": table,
                    "column": fk_col,
                    "orphan_row_count": orphan_count,
                    "orphan_sample": [r[0] for r in orphan_rows],
                    "fail": orphan_count > 0,
                }
            )
    return {
        "fact_tables_checked": fact_tables,
        "results": results,
        "pass": not any(r["fail"] for r in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Path to the dataset .duckdb file")
    args = parser.parse_args()

    con = duckdb.connect(args.dataset, read_only=True)
    summary = check_dataset(con)
    summary = {"dataset": args.dataset, **summary}
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
