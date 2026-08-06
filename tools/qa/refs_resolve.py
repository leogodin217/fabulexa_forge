#!/usr/bin/env python3
"""Referential-integrity checker — DATA-ONLY (duckdb + yaml + stdlib only).

Config-aware: validates only the columns the export config actually declares
as `fk:` (dimensional mode), each against ITS DECLARED TARGET table
(`fk.to`) — never "any dim in the dataset" and never a name heuristic like
"any `<x>_id` column in a `fact_*` table". Author-declared business
identifier columns (`from: prop__driver_id`, `from: presentation_id`, ...)
are not foreign keys and must not be flagged.

Per `docs/architecture/dimensional.md` ("Foreign keys — the labeled-edge
pathfind"), an `fk:` resolves to the target dim's grain `record_id`. What the
target *names* that column is the author's choice: it is whichever of the
target's columns declares `from: record_id`, and it is frequently not `id`
(`dim_patient` calls it `patient_id`). This script therefore reads the target
key column out of the config rather than assuming a name.

That assumption is exactly what rotted here once before: the script hard-coded
`id`, key election moved the name, and every dimensional example reported a
BinderException as though it were a referential defect. Hence the second rule
below — a checker that cannot run says so, and never says "defect".

base/source auto modes declare no `fk:` (they are mechanical prop__ ->
column maps, not FK-aware); for those this script reports "no declared FKs"
and passes cleanly rather than inventing FK semantics for them. That is an
absence of coverage, not evidence of integrity.

This script imports ONLY `duckdb`, `yaml` (pyyaml), and Python stdlib. It
must never import `fabulexa_forge` — it verifies exporter *output*, so
trusting the exporter's own code would make the check circular.

Usage:
    refs_resolve.py <config.yaml> <dataset.duckdb>

Exit codes:
    0  every declared FK resolves
    1  at least one declared FK has orphans (a real data defect)
    3  ungated -- the dataset could not be opened read-only, or one or more
       declared FKs could not be checked at all (target table absent from the
       config, no projected grain key, query error). NOT a data defect.
       Failures outrank ungateable: a run with both exits 1.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import yaml

#: Exit code for "could not gate". Distinct from 1 so a harness never reports
#: an unreadable dataset as an invariant violation.
UNGATED_EXIT = 3

#: The grain-key source column every dimensional table projects under a name of
#: the author's choosing. An `fk:` always points at this column of its target.
_GRAIN_KEY_SOURCE = "record_id"


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


def target_key_columns(cfg: dict) -> dict[str, list[str]]:
    """Return {table_name: [columns declaring `from: record_id`]}.

    A well-formed dimensional table projects its grain key exactly once, so each
    list normally holds one name. Zero and many are both reported rather than
    guessed at — see `resolve_target_key`.
    """
    return {
        tbl["name"]: [
            col["name"]
            for col in tbl["columns"]
            if col.get("from") == _GRAIN_KEY_SOURCE
        ]
        for tbl in cfg["dimensional"]["tables"]
    }


def resolve_target_key(
    target: str, keys_by_table: dict[str, list[str]]
) -> tuple[str | None, str | None]:
    """Resolve `target`'s grain-key column name.

    Returns:
        (column_name, None) when exactly one column projects the grain key, or
        (None, reason) when the FK cannot be checked. A reason is never a data
        defect — it means this gate has nothing to say about that edge.
    """
    if target not in keys_by_table:
        return None, (
            f"target table {target!r} is not declared in this config, so its key"
            " column is unknown"
        )
    keys = keys_by_table[target]
    if not keys:
        return None, (
            f"target table {target!r} projects no `from: {_GRAIN_KEY_SOURCE}`"
            " column, so it exposes no grain key to point at"
        )
    if len(keys) > 1:
        return None, (
            f"target table {target!r} projects `from: {_GRAIN_KEY_SOURCE}` under"
            f" {len(keys)} names ({', '.join(sorted(keys))}); which one an fk"
            " means is ambiguous"
        )
    return keys[0], None


def check_fk(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    target: str,
    target_key: str,
) -> dict:
    result = {
        "table": table,
        "column": column,
        "target": target,
        "target_key": target_key,
    }
    predicate = (
        f'from "{table}" where "{column}" is not null'
        f' and "{column}" not in (select "{target_key}" from "{target}")'
    )
    try:
        orphan_rows = con.execute(
            f'select distinct "{column}" {predicate} limit 5'
        ).fetchall()
        orphan_count = con.execute(f"select count(*) {predicate}").fetchone()[0]
    except duckdb.Error as exc:
        # A query that will not bind or run is a broken checker, not dirty data.
        # Reporting it as a failure is what trains people to ignore the matrix.
        return result | {
            "gated": False,
            "reason": f"query failed: {exc}",
            "fail": False,
        }
    return result | {
        "gated": True,
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
        keys_by_table = target_key_columns(cfg)
    elif mode in ("base", "source"):
        fk_maps = []
        keys_by_table = {}
    else:
        raise SystemExit(f"refs_resolve.py: unsupported export mode {mode!r}")

    results = []
    for table, column, target in fk_maps:
        target_key, reason = resolve_target_key(target, keys_by_table)
        if target_key is None:
            results.append(
                {
                    "table": table,
                    "column": column,
                    "target": target,
                    "gated": False,
                    "reason": reason,
                    "fail": False,
                }
            )
            continue
        results.append(check_fk(con, table, column, target, target_key))

    failures = [r for r in results if r["fail"]]
    ungated = [r for r in results if not r["gated"]]

    summary = {
        "config": args.config,
        "dataset": args.dataset,
        "mode": mode,
        "declared_fks": len(results),
        "fks_checked": len(results) - len(ungated),
        "fks_ungated": len(ungated),
        "results": results,
        "pass": not failures,
    }
    if not results:
        summary["note"] = (
            f"{mode} mode declares no `fk:` columns; this gate checked nothing."
            " Absence of coverage, not evidence of integrity."
        )
    print(json.dumps(summary, indent=2, default=str))

    if failures:
        return 1
    return UNGATED_EXIT if ungated else 0


if __name__ == "__main__":
    raise SystemExit(main())
