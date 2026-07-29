#!/usr/bin/env python3
"""Referential-integrity checker — DATA-ONLY (duckdb + yaml + stdlib only).

Config-aware: validates only the columns the export config actually declares
as `fk:` (dimensional mode), each against ITS DECLARED TARGET table
(`fk.to`) — never "any dim in the dataset" and never a name heuristic like
"any `<x>_id` column in a `fact_*` table". Author-declared business
identifier columns (`from: prop__driver_id`, `from: presentation_id`, ...)
are not foreign keys and must not be flagged.

Per `docs/architecture/dimensional.md` ("Foreign keys — the labeled-edge
pathfind"), an `fk:` always resolves to the target dim's grain `record_id`,
which the dim table always projects as its `id` column (`{name: id, from:
record_id}`) — so the target key column is always `id`.

base/source auto modes declare no `fk:` (they are mechanical prop__ ->
column maps, not FK-aware); for those this script reports "no declared FKs"
and passes cleanly rather than inventing FK semantics for them.

This script imports ONLY `duckdb`, `yaml` (pyyaml), and Python stdlib. It
must never import `fabulexa_forge` — it verifies exporter *output*, so
trusting the exporter's own code would make the check circular.

Usage:
    refs_resolve.py <config.yaml> <dataset.duckdb>

Exit codes:
    0  every declared FK resolves
    1  at least one declared FK has orphans (a real data defect)
    3  ungated -- the dataset could not be opened read-only (locked by a
       concurrent writer, mid-write, or corrupt). NOT a data defect.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import yaml

#: Exit code for "could not gate". Distinct from 1 so a harness never reports
#: an unreadable dataset as an invariant violation.
UNGATED_EXIT = 3


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def dimensional_fk_maps(cfg: dict) -> list[tuple[str, str, str]]:
    """Return [(table, column, target_table)] for every `fk:`-declared column
    in dimensional mode."""
    out = []
    for tbl in cfg["dimensional"]["tables"]:
        table = tbl["name"]
        for col in tbl["columns"]:
            if "fk" in col:
                out.append((table, col["name"], col["fk"]["to"]))
    return out


def check_fk(
    con: duckdb.DuckDBPyConnection, table: str, column: str, target: str
) -> dict:
    orphan_rows = con.execute(
        f"""
        select distinct "{column}"
        from "{table}"
        where "{column}" is not null
          and "{column}" not in (select id from "{target}")
        limit 5
        """
    ).fetchall()
    orphan_count = con.execute(
        f"""
        select count(*)
        from "{table}"
        where "{column}" is not null
          and "{column}" not in (select id from "{target}")
        """
    ).fetchone()[0]
    return {
        "table": table,
        "column": column,
        "target": target,
        "orphan_row_count": orphan_count,
        "orphan_sample": [r[0] for r in orphan_rows],
        "fail": orphan_count > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Path to the export config YAML")
    parser.add_argument("dataset", help="Path to the dataset .duckdb file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    mode = cfg["mode"]

    try:
        con = duckdb.connect(args.dataset, read_only=True)
    except duckdb.Error as exc:
        print(
            json.dumps(
                {
                    "dataset": args.dataset,
                    "gated": False,
                    "reason": f"dataset could not be opened read-only: {exc}",
                },
                indent=2,
            )
        )
        return UNGATED_EXIT

    if mode == "dimensional":
        fk_maps = dimensional_fk_maps(cfg)
    elif mode in ("base", "source"):
        fk_maps = []
    else:
        raise SystemExit(f"refs_resolve.py: unsupported export mode {mode!r}")

    results = [
        check_fk(con, table, column, target) for table, column, target in fk_maps
    ]

    summary = {
        "config": args.config,
        "dataset": args.dataset,
        "mode": mode,
        "declared_fks_checked": len(results),
        "results": results,
        "pass": not any(r["fail"] for r in results),
    }
    if not results:
        summary["note"] = "no declared FKs for this mode"
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
