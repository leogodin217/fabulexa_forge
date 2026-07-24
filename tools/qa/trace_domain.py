#!/usr/bin/env python3
"""No-fabrication ("faithful reshaping") checker — DATA-ONLY.

Imports ONLY `duckdb`, `yaml` (pyyaml), and Python stdlib. It must never
import `fabulexa_forge` — it verifies exporter *output* against the raw
base-layer bundle, so trusting the exporter's own code would make the check
circular.

Parses the export config (YAML) to map each output table -> its grain source
table, and each output column -> its source column:

  - dimensional mode: `{name: X, from: prop__Y}` / `{name: id, from: record_id}`
    (any literal `from:` value is a column name on that table's GRAIN surface).
    The grain decides which bundle table that is -- `records__<kind>` for a
    records grain, `membership__<kind>__<property>` for a membership grain,
    `history` for the history grains (see `grain_source`). Columns with
    `derived:` or `fk:` are skipped -- not direct traces; so is the virtual
    `lead_sim_time`, which exists in no bundle table.
  - base/source auto modes: the mechanical map `id` -> `record_id`,
    and `X` -> `prop__X` (base mode keeps the `prop__` prefix in the output
    column name; source mode strips it). The kind list is every
    `records__<kind>` table in the bundle minus `<mode>.exclude.kinds`.

For each mapped, non-derived column, asserts that every DISTINCT output
value exists in the source column's domain. The source domain is the UNION
of:
  (1) the current-state value in `records__<kind>.<col>` (every live record's
      present value), and
  (2) every value that property ever took, per the `history` long-form log
      (`history.value` where `kind` = <kind> and `property` = the property
      name without its `prop__` prefix) -- this is necessary because SCD-2
      dimensional output rows carry PAST states, not just the current one,
      and the base layer's only record of past states is `history`.
Both are legitimate base-layer values; a value present in neither was
fabricated by the exporter.

Usage:
    trace_domain.py <config.yaml> <dataset.duckdb> <bundle_run.duckdb>
"""

from __future__ import annotations

import argparse
import json

import duckdb
import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


#: Grain-source columns that are virtual (engine-computed, not in any bundle
#: table), so there is no source column to trace them against.
_VIRTUAL_COLS = frozenset({"lead_sim_time"})


def grain_source(src: dict) -> tuple[str, str | None]:
    """Return (bundle_table, history_property) for a dimensional table's source.

    A dimensional table's grain decides which bundle table its `from:` columns
    are projected off (dimensional.md § "Projectable columns per grain") -- it is
    NOT always `records__<kind>`:

      records                        -> records__<kind>
      membership                     -> membership__<kind>__<property>
      history_point/history_interval -> history (filtered to kind[, property])
    """
    grain = src.get("grain", "records")
    kind = src["kind"]
    if grain == "membership":
        return f"membership__{kind}__{src['property']}", None
    if grain in ("history_point", "history_interval"):
        return "history", src.get("property")
    return f"records__{kind}", None


def dimensional_column_maps(cfg: dict) -> list[tuple[str, dict, dict[str, str]]]:
    """Return [(output_table, source_decl, {output_col: source_col})] for
    dimensional mode."""
    out = []
    for tbl in cfg["dimensional"]["tables"]:
        name = tbl["name"]
        src = tbl["source"]
        col_map = {}
        for col in tbl["columns"]:
            if "from" in col and col["from"] not in _VIRTUAL_COLS:
                col_map[col["name"]] = col["from"]
            # `derived:` and `fk:` columns are skipped -- not direct traces.
        out.append((name, src, col_map))
    return out


def auto_column_maps(
    cfg: dict, mode: str, con: duckdb.DuckDBPyConnection
) -> list[tuple[str, dict, dict[str, str]]]:
    """Return [(output_table, source_decl, {output_col: source_col})] for base/source
    auto modes. `con` must have the bundle attached as `src` and the dataset as
    `out`. Auto modes are always a records grain."""
    exclude = set(cfg.get(mode, {}).get("exclude", {}).get("kinds", []) or [])
    bundle_tables = [
        row[0]
        for row in con.execute(
            "select table_name from duckdb_tables() where database_name = 'src'"
        ).fetchall()
    ]
    kinds = [
        t.removeprefix("records__") for t in bundle_tables if t.startswith("records__")
    ]
    kinds = [k for k in kinds if k not in exclude]

    out_tables = {
        row[0]
        for row in con.execute(
            "select table_name from duckdb_tables() where database_name = 'out'"
        ).fetchall()
    }

    result = []
    for kind in kinds:
        output_table = kind
        if output_table not in out_tables:
            # e.g. junction/membership-derived output tables not named after a kind.
            continue
        bundle_cols = {
            row[0] for row in con.execute(f"describe src.records__{kind}").fetchall()
        }
        out_cols = [
            row[0] for row in con.execute(f"describe out.{output_table}").fetchall()
        ]
        col_map = {}
        for oc in out_cols:
            if oc == "id":
                col_map[oc] = "record_id"
            elif oc.startswith("prop__") and oc in bundle_cols:
                col_map[oc] = oc  # base mode: identity, prop__ prefix kept
            elif f"prop__{oc}" in bundle_cols:
                col_map[oc] = f"prop__{oc}"  # source mode: prefix stripped
        result.append((output_table, {"grain": "records", "kind": kind}, col_map))
    return result


def check_column(
    con: duckdb.DuckDBPyConnection,
    output_table: str,
    source: dict,
    out_col: str,
    source_col: str,
) -> dict:
    kind = source["kind"]
    src_table, hist_prop = grain_source(source)
    where = ""
    if src_table == "history":
        where = f" where kind = '{kind}'" + (
            f" and property = '{hist_prop}'" if hist_prop else ""
        )
    domain_parts = [
        f'select distinct cast("{source_col}" as varchar) from src.{src_table}{where}'
    ]
    # Only a records-grain prop__ column carries past states in `history`; a
    # membership/history grain row IS the historical fact, already in its table.
    if source_col.startswith("prop__") and src_table.startswith("records__"):
        prop_name = source_col.removeprefix("prop__")
        domain_parts.append(
            "select distinct value from src.history "
            f"where kind = '{kind}' and property = '{prop_name}'"
        )
    domain_sql = " union ".join(domain_parts)

    fabricated = con.execute(
        f"""
        select distinct cast("{out_col}" as varchar) as v
        from out."{output_table}"
        where "{out_col}" is not null
        except
        ({domain_sql})
        """
    ).fetchall()

    return {
        "table": output_table,
        "column": out_col,
        "source_kind": kind,
        "source_table": src_table,
        "source_column": source_col,
        "fabricated_value_count": len(fabricated),
        "fabricated_sample": [r[0] for r in fabricated[:5]],
        "fail": len(fabricated) > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Path to the export config YAML")
    parser.add_argument("dataset", help="Path to the output dataset .duckdb file")
    parser.add_argument("bundle", help="Path to the base bundle run.duckdb file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    mode = cfg["mode"]

    con = duckdb.connect(":memory:")
    con.execute(f"attach '{args.bundle}' as src (read_only)")
    con.execute(f"attach '{args.dataset}' as out (read_only)")

    if mode == "dimensional":
        table_maps = dimensional_column_maps(cfg)
    elif mode in ("base", "source"):
        table_maps = auto_column_maps(cfg, mode, con)
    else:
        raise SystemExit(f"trace_domain.py: unsupported export mode {mode!r}")

    results = []
    for output_table, source, col_map in table_maps:
        for out_col, source_col in col_map.items():
            results.append(check_column(con, output_table, source, out_col, source_col))

    summary = {
        "config": args.config,
        "dataset": args.dataset,
        "bundle": args.bundle,
        "mode": mode,
        "columns_checked": len(results),
        "failures": [r for r in results if r["fail"]],
        "pass": not any(r["fail"] for r in results),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
