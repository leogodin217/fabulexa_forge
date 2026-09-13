"""Run a billing query against the pack's dimensional export and write the result.

    uv run python exercises/run_query.py <query.sql> <out.duckdb> [--table NAME]

The export is opened read-only; the result lands as one table (default
`monthly_bill`) in a fresh `out.duckdb`, ready for `fabulexa-forge compare`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
EXPORT = HERE.parent / "exports" / "dimensional.duckdb"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--table", default="monthly_bill")
    args = ap.parse_args()

    if args.out.exists():
        args.out.unlink()
    con = duckdb.connect(str(args.out))
    out_db = con.execute("SELECT current_database()").fetchone()[0]
    con.execute(f"ATTACH '{EXPORT}' AS src (READ_ONLY)")
    con.execute("USE src")
    t0 = time.perf_counter()
    con.execute(f'CREATE TABLE "{out_db}"."{args.table}" AS ' + args.query.read_text())
    n = con.execute(f'SELECT count(*) FROM "{out_db}"."{args.table}"').fetchone()[0]
    con.close()
    print(f"{args.table}: {n} rows in {time.perf_counter() - t0:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
