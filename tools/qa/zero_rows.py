#!/usr/bin/env python3
"""Zero-row tripwire — DATA-ONLY (duckdb + yaml + stdlib only).

Fails when any table in the exported dataset carries zero rows, unless the
table is named on an *expected-empty allowlist*. Also fails when the dataset
holds no tables at all.

Why this gate exists: an export can write a table with the right columns and
no rows (a `where` that matches nothing, a sub-type with no live records, a
membership that never fires) and every other gate passes vacuously — no
SCD-2 windows to violate, no FKs to dangle, no values to trace. The empty
table is then first noticed by whoever compares the render to the previous
publication. This gate makes "a table is empty" a verdict of its own.

The allowlist is a YAML file beside the example's export configs
(`docs/examples/<example>/expected-empty.yaml` by convention), keyed by
export mode, each entry a table name mapped to the reason it is expected to
be empty:

    dimensional:
      fact_refund: "the scenario declares refunds but none fire in this horizon"
    source:
      refund: "same population as dimensional's fact_refund"

The reason is required and must be non-empty — an allowlisted table is an
author's claim about the data, and the claim must say why. An allowlisted
table that turns out non-empty (or absent) is reported as a *stale* entry
under `warnings`: not a data defect, but an allowlist that no longer
describes the dataset and should be pruned.

This script imports ONLY `duckdb`, `yaml` (pyyaml), and Python stdlib. It
must never import `fabulexa_forge` — it verifies exporter *output*, so
trusting the exporter's own code would make the check circular.

Usage:
    zero_rows.py <config.yaml> <dataset.duckdb> [--expected-empty <allowlist.yaml>]

Exit codes:
    0  every table carries rows, or is allowlisted as expected-empty
    1  at least one non-allowlisted table has zero rows, or the dataset has
       no tables (a real data defect)
    3  ungated -- the dataset could not be opened read-only, or the allowlist
       file is malformed. NOT a data defect.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import yaml

#: Exit code for "could not gate". Distinct from 1 so a harness never reports
#: an unreadable dataset as an invariant violation.
UNGATED_EXIT = 3


def load_yaml(path: str) -> object:
    with open(path) as f:
        return yaml.safe_load(f)


def load_allowlist(path: str | None, mode: str) -> dict[str, str]:
    """Return {table: reason} for `mode` from the allowlist file, or {} when
    no file was given or the file names nothing for this mode.

    Raises ValueError when the file's shape is not mode -> {table: reason}
    with a non-empty string reason per table."""
    if path is None:
        return {}
    doc = load_yaml(path)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping of mode -> tables")
    section = doc.get(mode)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{path}: section {mode!r} must map table -> reason")
    out: dict[str, str] = {}
    for table, reason in section.items():
        if not isinstance(table, str) or not isinstance(reason, str) or not reason:
            raise ValueError(
                f"{path}: {mode}.{table!r} must map a table name to a non-empty"
                " string reason"
            )
        out[table] = reason
    return out


def count_rows(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    tables = [row[0] for row in con.execute("show tables").fetchall()]
    return {
        table: con.execute(f'select count(*) from "{table}"').fetchone()[0]
        for table in sorted(tables)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Path to the export config YAML")
    parser.add_argument("dataset", help="Path to the dataset .duckdb file")
    parser.add_argument(
        "--expected-empty",
        dest="expected_empty",
        default=None,
        help="Allowlist YAML: mode -> {table: reason} for tables expected empty",
    )
    args = parser.parse_args()

    cfg = load_yaml(args.config)
    mode = cfg["mode"]

    try:
        allowlist = load_allowlist(args.expected_empty, mode)
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "dataset": args.dataset,
                    "gated": False,
                    "reason": f"expected-empty allowlist is malformed: {exc}",
                },
                indent=2,
            )
        )
        return UNGATED_EXIT

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

    counts = count_rows(con)

    empty = sorted(t for t, n in counts.items() if n == 0)
    failures = [t for t in empty if t not in allowlist]
    expected_empty = [t for t in empty if t in allowlist]
    warnings = []
    for table, reason in sorted(allowlist.items()):
        if table not in counts:
            warnings.append(
                f"stale allowlist entry: {table!r} is not in the dataset ({reason})"
            )
        elif counts[table] > 0:
            warnings.append(
                f"stale allowlist entry: {table!r} has {counts[table]} rows ({reason})"
            )

    no_tables = not counts

    summary = {
        "config": args.config,
        "dataset": args.dataset,
        "mode": mode,
        "expected_empty_file": args.expected_empty,
        "tables": len(counts),
        "row_counts": counts,
        "zero_row_tables": failures,
        "expected_empty_tables": expected_empty,
        "warnings": warnings,
        "pass": not failures and not no_tables,
    }
    if no_tables:
        summary["note"] = "the dataset holds no tables at all -- nothing was exported"
    print(json.dumps(summary, indent=2, default=str))

    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
