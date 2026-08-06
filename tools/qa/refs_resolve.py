#!/usr/bin/env python3
"""Referential-integrity checker — DATA-ONLY (duckdb + yaml + stdlib only).

Config-aware: validates only the columns the export config actually declares
as `fk:` (dimensional mode), each against ITS DECLARED TARGET table
(`fk.to`) — never "any dim in the dataset" and never a name heuristic like
"any `<x>_id` column in a `fact_*` table". Author-declared business
identifier columns (`from: prop__driver_id`, `from: presentation_id`, ...)
are not foreign keys and must not be flagged.

Per `docs/architecture/dimensional.md` ("Foreign keys — the labeled-edge
pathfind") and `key-election.md`, an `fk:` resolves to the target dim's
ELECTED identity surface: `record_id` by default, or whichever of
`record_id` / `record_index` / `presentation_id` the config's `keys:` block
elects for the target's source population — overridable per edge by
`fk.target_key`. What the target *names* that column is the author's choice:
it is whichever of the target's columns declares `from: <that surface>`, and
it is frequently not `id` (`dim_patient` calls it `patient_id`). This script
therefore resolves the surface the way the exporter does (target_key wins;
else the election inherited from the target dim's population set; else
`record_id`) and reads the key column name out of the config rather than
assuming either.

That discipline exists because this script rotted twice along the same axis:
first it hard-coded the key column NAME `id`, then it hard-coded the key
column SOURCE `from: record_id` — and key election moved both out from under
it, turning checkable edges into BinderExceptions or UNGATED reports. Hence
the second rule below — a checker that cannot run says so, and never says
"defect".

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

#: The default identity surface a population presents when the config's
#: `keys:` block elects nothing for it.
_DEFAULT_SURFACE = "record_id"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def dimensional_fk_maps(cfg: dict) -> list[tuple[str, str, dict]]:
    """Return [(table, column, fk_decl)] for every `fk:`-declared column
    in dimensional mode. `fk_decl` is the raw fk mapping (carries `to` and
    the optional per-edge `target_key` override)."""
    out = []
    for tbl in cfg["dimensional"]["tables"]:
        table = tbl["name"]
        for col in tbl["columns"]:
            if "fk" in col:
                out.append((table, col["name"], col["fk"]))
    return out


def elected_surface(cfg: dict, target_decl: dict) -> tuple[str | None, str | None]:
    """Resolve the identity surface `target_decl`'s population set elects.

    Mirrors the exporter's inheritance rule (key-election.md): no `keys:`
    entry for the kind -> record_id; a scalar election -> that surface for
    every population of the kind; a per-sub-type map -> resolved over the
    dim's own sub-type population set (its `prop__<kind>_type` filter),
    unlisted sub-types defaulting to record_id.

    Returns:
        (surface, None) when one coherent surface resolves, or (None, reason)
        when this checker cannot know it (no filter to name the population
        set, or the set elects differing surfaces). A reason is never a data
        defect.
    """
    source = target_decl.get("source") or {}
    kind = source.get("kind")
    election = (cfg.get("keys") or {}).get(kind)
    if election is None:
        return _DEFAULT_SURFACE, None
    if isinstance(election, str):
        return election, None
    # Per-sub-type map: the surface depends on which sub-types the dim holds.
    discriminator = f"prop__{kind}_type"
    sub_types = (source.get("filter") or {}).get(discriminator)
    if sub_types is None:
        return None, (
            f"kind {kind!r} carries a per-sub-type `keys:` election but the"
            f" target dim declares no {discriminator} filter, so its population"
            " set (and elected surface) is unknown to this checker"
        )
    if not isinstance(sub_types, list):
        sub_types = [sub_types]
    resolved = {s: election.get(s, _DEFAULT_SURFACE) for s in sub_types}
    surfaces = set(resolved.values())
    if len(surfaces) > 1:
        pairs = ", ".join(f"{s}={v}" for s, v in sorted(resolved.items()))
        return None, (
            f"target dim's population set for kind {kind!r} elects differing"
            f" surfaces ({pairs}); nothing coherent to inherit"
        )
    return surfaces.pop(), None


def resolve_target_key(
    fk_decl: dict, tables_by_name: dict[str, dict], cfg: dict
) -> tuple[tuple[str, str] | None, str | None]:
    """Resolve the target's key surface and key column name for one fk edge.

    Returns:
        ((surface, column_name), None) when the edge's surface resolves and
        exactly one target column projects it, or (None, reason) when the FK
        cannot be checked. A reason is never a data defect — it means this
        gate has nothing to say about that edge.
    """
    target = fk_decl["to"]
    if target not in tables_by_name:
        return None, (
            f"target table {target!r} is not declared in this config, so its key"
            " column is unknown"
        )
    surface = fk_decl.get("target_key")
    if surface is None:
        surface, reason = elected_surface(cfg, tables_by_name[target])
        if surface is None:
            return None, reason
    keys = [
        col["name"]
        for col in tables_by_name[target]["columns"]
        if col.get("from") == surface
    ]
    if not keys:
        return None, (
            f"target table {target!r} projects no `from: {surface}`"
            " column, so it exposes no key in the edge's elected surface"
        )
    if len(keys) > 1:
        return None, (
            f"target table {target!r} projects `from: {surface}` under"
            f" {len(keys)} names ({', '.join(sorted(keys))}); which one an fk"
            " means is ambiguous"
        )
    return (surface, keys[0]), None


def check_fk(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    target: str,
    target_key: str,
    surface: str,
) -> dict:
    result = {
        "table": table,
        "column": column,
        "target": target,
        "target_key": target_key,
        "surface": surface,
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
        tables_by_name = {tbl["name"]: tbl for tbl in cfg["dimensional"]["tables"]}
    elif mode in ("base", "source"):
        fk_maps = []
        tables_by_name = {}
    else:
        raise SystemExit(f"refs_resolve.py: unsupported export mode {mode!r}")

    results = []
    for table, column, fk_decl in fk_maps:
        resolved, reason = resolve_target_key(fk_decl, tables_by_name, cfg)
        if resolved is None:
            results.append(
                {
                    "table": table,
                    "column": column,
                    "target": fk_decl["to"],
                    "gated": False,
                    "reason": reason,
                    "fail": False,
                }
            )
            continue
        surface, target_key = resolved
        results.append(check_fk(con, table, column, fk_decl["to"], target_key, surface))

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
