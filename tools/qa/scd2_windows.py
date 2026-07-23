#!/usr/bin/env python3
"""SCD-2 window sanity checker — DATA-ONLY (duckdb + stdlib only).

For every table in the given dataset that has an `id`, `valid_from`, and
`valid_to` column, checks (per `id`, ordered by `valid_from`):

  (a) valid_from < valid_to for every row              -> FAIL
  (b) no overlapping intervals within a key             -> FAIL
  (c) exactly one open/current row per key               -> FAIL
  (d) gaps between consecutive intervals                 -> WARNING (not a failure)

The "open row" convention (NULL `valid_to` vs. a max-sentinel value) is
detected from the data itself, not assumed.

This script imports ONLY `duckdb` + Python stdlib. It must never import
`fabulexa_forge` — it verifies exporter *output*, so trusting the exporter's
own code would make the check circular.

Usage:
    scd2_windows.py <dataset.duckdb>
"""

from __future__ import annotations

import argparse
import json

import duckdb


def find_scd2_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    tables = [row[0] for row in con.execute("show tables").fetchall()]
    scd2_tables = []
    for table in tables:
        cols = {row[0] for row in con.execute(f"describe {table}").fetchall()}
        if {"id", "valid_from", "valid_to"} <= cols:
            scd2_tables.append(table)
    return scd2_tables


def detect_open_convention(
    con: duckdb.DuckDBPyConnection, table: str
) -> tuple[str, object]:
    """Detect whether the open/current row is marked by NULL valid_to or a
    max-sentinel value. Returns (convention, sentinel_value)."""
    null_count = con.execute(
        f"select count(*) from {table} where valid_to is null"
    ).fetchone()[0]
    if null_count > 0:
        return "null", None
    sentinel = con.execute(f"select max(valid_to) from {table}").fetchone()[0]
    return "sentinel", sentinel


def check_table(con: duckdb.DuckDBPyConnection, table: str) -> dict:
    convention, sentinel = detect_open_convention(con, table)
    is_open_expr = (
        "valid_to is null" if convention == "null" else f"valid_to = {sentinel!r}"
    )

    result = {
        "table": table,
        "open_convention": convention,
        "open_sentinel": str(sentinel) if sentinel is not None else None,
    }

    # (a) inverted intervals: valid_from >= valid_to (excluding the open row,
    # which by definition has no meaningful upper bound under the sentinel
    # convention either — still check it, a sentinel must legitimately be > valid_from).
    inverted = con.execute(
        f"""
        select id, valid_from, valid_to
        from {table}
        where valid_to is not null and valid_from >= valid_to
        limit 5
        """
    ).fetchall()
    inverted_count = con.execute(
        f"""
        select count(*) from {table}
        where valid_to is not null and valid_from >= valid_to
        """
    ).fetchone()[0]
    result["inverted_intervals"] = {
        "count": inverted_count,
        "sample": [
            {"id": r[0], "valid_from": str(r[1]), "valid_to": str(r[2])}
            for r in inverted
        ],
    }

    # (b) overlaps + (d) gaps: compare each row to the next row (by valid_from), per id.
    windowed = f"""
        with ordered as (
            select
                id, valid_from, valid_to,
                lead(valid_from) over (
                    partition by id order by valid_from
                ) as next_valid_from,
                {is_open_expr} as is_open
            from {table}
        )
    """
    overlaps = con.execute(
        windowed
        + """
        select id, valid_from, valid_to, next_valid_from
        from ordered
        where next_valid_from is not null
          and not is_open
          and next_valid_from < valid_to
        limit 5
        """
    ).fetchall()
    overlap_count = con.execute(
        windowed
        + """
        select count(*) from ordered
        where next_valid_from is not null
          and not is_open
          and next_valid_from < valid_to
        """
    ).fetchone()[0]
    result["overlaps"] = {
        "count": overlap_count,
        "sample": [
            {
                "id": r[0],
                "valid_from": str(r[1]),
                "valid_to": str(r[2]),
                "next_valid_from": str(r[3]),
            }
            for r in overlaps
        ],
    }

    gaps = con.execute(
        windowed
        + """
        select id, valid_to, next_valid_from
        from ordered
        where next_valid_from is not null
          and not is_open
          and next_valid_from != valid_to
        limit 5
        """
    ).fetchall()
    gap_count = con.execute(
        windowed
        + """
        select count(*) from ordered
        where next_valid_from is not null
          and not is_open
          and next_valid_from != valid_to
        """
    ).fetchone()[0]
    result["gaps"] = {
        "count": gap_count,
        "sample": [
            {"id": r[0], "valid_to": str(r[1]), "next_valid_from": str(r[2])}
            for r in gaps
        ],
    }

    # (c) exactly one open row per key.
    open_counts = con.execute(
        f"""
        select id, count(*) as n_open
        from {table}
        where {is_open_expr}
        group by id
        having count(*) != 1
        limit 5
        """
    ).fetchall()
    zero_open = con.execute(
        f"""
        select count(*) from (
            select id from {table}
            group by id
            having sum(case when {is_open_expr} then 1 else 0 end) = 0
        )
        """
    ).fetchone()[0]
    multi_open = con.execute(
        f"""
        select count(*) from (
            select id from {table}
            group by id
            having sum(case when {is_open_expr} then 1 else 0 end) > 1
        )
        """
    ).fetchone()[0]
    result["open_row_violations"] = {
        "zero_open_keys": zero_open,
        "multi_open_keys": multi_open,
        "sample": [{"id": r[0], "n_open": r[1]} for r in open_counts],
    }

    result["fail"] = bool(
        result["inverted_intervals"]["count"]
        or result["overlaps"]["count"]
        or zero_open
        or multi_open
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Path to the dataset .duckdb file")
    args = parser.parse_args()

    con = duckdb.connect(args.dataset, read_only=True)
    tables = find_scd2_tables(con)

    results = [check_table(con, t) for t in tables]
    overall_fail = any(r["fail"] for r in results)

    summary = {
        "dataset": args.dataset,
        "scd2_tables_checked": tables,
        "results": results,
        "pass": not overall_fail,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 1 if overall_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
