#!/usr/bin/env python
"""
Demo: `init` keys proposal (dimensional) — `generate_init_config` proposes a
`keys` block alongside the table stubs: `presentation_id` where the registry
declares a population, `record_index` elsewhere; scalar/map shape mirroring
the registry; self-gated through `resolve_election` +
`check_edge_union_safety` over the emit's reference graph, with degradation
to uniform `record_index` and a YAML comment naming the forcing gate; dim key
proposals source `from:` the elected surface, subsuming the natural-key
advisory where the election is `presentation_id`
(`exporters/dimensional/init.py`).

Sprint: key-election
Phase: 7

Builds one emit with three kinds exercising the three demo scenarios:

  - `location` — flat, fully declared in `presentation_keys` -> a clean
    `presentation_id` scalar proposal, dim id column aligned, advisory
    comment subsumed.
  - `entity` — sub-typed (`alpha` / `beta`); only `alpha` is registry-declared
    -> a per-sub-type map proposal (`alpha: presentation_id`, `beta:
    record_index`).
  - `sibling` — sub-typed (`p` / `q`), BOTH declared on a bare (comparable)
    counter prefix — pairwise union-unsafe. `trip.prop__sibling_id`
    references `sibling`, giving the self-gate an edge to check: the natural
    proposal fails `check_edge_union_safety`, so `sibling` degrades to the
    uniform `record_index` scalar with a comment naming
    `ElectionUnionUnsafe`.

The emitted YAML is then run back through `resolve_election` +
`validate_table` (the exact machinery `init` self-gated against) to prove
the proposal never fails its own gates.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.reader.emit import open_emit

_LOCATION_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
]

_ENTITY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__entity_type", "type": "VARCHAR"},
]

_SIBLING_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__sibling_type", "type": "VARCHAR"},
]

_TRIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__sibling_id", "type": "VARCHAR", "references": "sibling"},
    {"name": "ref_index__sibling_id", "type": "BIGINT"},
]

_LOCATION_ROWS: list[tuple[object, ...]] = [
    ("trunk", "loc1", "LOC_001", 10, True, None, 10, 0),
]

# entity: e1 alpha (registry-declared ALPHA_001), e2 beta (undeclared, NULL).
_ENTITY_ROWS: list[tuple[object, ...]] = [
    ("trunk", "e1", "ALPHA_001", 10, True, None, 10, 0, "alpha"),
    ("trunk", "e2", None, 10, True, None, 10, 1, "beta"),
]

# sibling: s1/p and s2/q, both declared on the SAME bare counter prefix.
_SIBLING_ROWS: list[tuple[object, ...]] = [
    ("trunk", "s1", "001", 10, True, None, 10, 0, "p"),
    ("trunk", "s2", "002", 10, True, None, 10, 1, "q"),
]

_TRIP_ROWS: list[tuple[object, ...]] = [
    ("trunk", "t1", 20, True, None, 20, 0, "s1", 0),
]

_RECORD_ROLES: dict[str, object] = {
    "location": "dimension",
    "entity": {"alpha": "dimension", "beta": "dimension"},
    "sibling": {"p": "dimension", "q": "dimension"},
    "trip": "fact",
}

_PRESENTATION_KEYS: dict[str, object] = {
    "location": {
        "key": {
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
            "key_space": {"class": "record_index", "prefix": "LOC_", "width": 4},
        }
    },
    # Only alpha declared: the rollup is a singleton claim over alpha alone.
    "entity": {
        "sub_types": {
            "alpha": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "ALPHA_", "width": 3},
            }
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    },
    # p and q share the SAME bare counter prefix — pairwise union-unsafe, so
    # the rollup derives no claim (unique_within: None).
    "sibling": {
        "sub_types": {
            "p": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
            "q": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
        },
        "branch_stable": False,
        "slice_stable": False,
    },
}


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _insert_all(
    conn: "duckdb.DuckDBPyConnection",
    table: str,
    cols: list[dict[str, object]],
    rows: list[tuple[object, ...]],
) -> None:
    placeholders = ", ".join("?" for _ in cols)
    for row in rows:
        conn.execute(f'INSERT INTO "{table}" VALUES ({placeholders})', list(row))


def _build_emit(emit_dir: Path) -> None:
    """Write the location / entity / sibling / trip emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(_ddl("records__entity", _ENTITY_COLUMNS))
    conn.execute(_ddl("records__sibling", _SIBLING_COLUMNS))
    conn.execute(_ddl("records__trip", _TRIP_COLUMNS))
    _insert_all(conn, "records__location", _LOCATION_COLUMNS, _LOCATION_ROWS)
    _insert_all(conn, "records__entity", _ENTITY_COLUMNS, _ENTITY_ROWS)
    _insert_all(conn, "records__sibling", _SIBLING_COLUMNS, _SIBLING_ROWS)
    _insert_all(conn, "records__trip", _TRIP_COLUMNS, _TRIP_ROWS)
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": "records__location",
                "category": "records",
                "record_kind": "location",
                "columns": _LOCATION_COLUMNS,
                "rows": len(_LOCATION_ROWS),
            },
            {
                "name": "records__entity",
                "category": "records",
                "record_kind": "entity",
                "columns": _ENTITY_COLUMNS,
                "rows": len(_ENTITY_ROWS),
            },
            {
                "name": "records__sibling",
                "category": "records",
                "record_kind": "sibling",
                "columns": _SIBLING_COLUMNS,
                "rows": len(_SIBLING_ROWS),
            },
            {
                "name": "records__trip",
                "category": "records",
                "record_kind": "trip",
                "columns": _TRIP_COLUMNS,
                "rows": len(_TRIP_ROWS),
            },
        ],
        "record_roles": _RECORD_ROLES,
        "enum_domains": {
            "entity": {"entity_type": ["alpha", "beta"]},
            "sibling": {"sibling_type": ["p", "q"]},
        },
        "presentation_keys": _PRESENTATION_KEYS,
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> "SystemExit":
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp) / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            content = generate_init_config(emit, lambda _n: None)

        out_path = Path(tmp) / "candidate.yaml"
        out_path.write_text(content, encoding="utf-8")

        # ---- 1. Fully-declared: clean presentation_id scalar, aligned key ----
        print("=== location: fully declared -> presentation_id scalar ===")
        if "keys:\n  location: presentation_id" not in content:
            raise _fail("expected `location: presentation_id` in the keys: block")
        if "{name: id, from: presentation_id}" not in content:
            raise _fail("expected dim_location's id column aligned to presentation_id")
        if "presentation_id` a natural key for 'location'" in content:
            raise _fail("advisory comment should be subsumed on dim_location's stub")
        print(
            "  OK: keys.location = presentation_id, dim id aligned, advisory subsumed"
        )
        print()

        # ---- 2. Partially-declared: per-sub-type map, record_index fallback --
        print("=== entity: alpha declared / beta undeclared -> per-sub-type map ===")
        if (
            "  entity:\n    alpha: presentation_id\n    beta: record_index"
            not in content
        ):
            raise _fail("expected entity's per-sub-type map with the beta fallback")
        print("  OK: keys.entity = {alpha: presentation_id, beta: record_index}")
        print()

        # ---- 3. Bare-counter siblings: degrade + comment naming the gate -----
        print("=== sibling: bare-counter siblings referenced by trip -> degrade ===")
        if "sibling: record_index  # NOTE: ElectionUnionUnsafe" not in content:
            raise _fail("expected sibling degraded with an ElectionUnionUnsafe comment")
        if (
            "{name: id, from: record_index}"
            not in content.split("dim_sibling_p")[1][:400]
        ):
            raise _fail("expected dim_sibling_p's id column degraded to record_index")
        print("  OK: keys.sibling = record_index  # ... ElectionUnionUnsafe ...")
        print()

        # ---- The proposal never fails its own gates -------------------------
        print("=== the emitted YAML passes resolve_election + validate_table ===")
        config = load_export_config(out_path)
        assert config.dimensional is not None
        with open_emit(emit_dir) as emit:
            election = resolve_election(emit.sidecar, config.keys)
            for table_decl in config.dimensional.tables:
                validate_table(
                    table_decl,
                    config.dimensional,
                    emit.sidecar,
                    None,
                    lambda _n: None,
                    election=election,
                )
        print("  OK: every proposed table validates under its own proposed election")
        print()

        print(
            "SUCCESS: init proposes presentation_id where the registry declares"
            " the population and record_index elsewhere, self-gates the proposal"
            " through resolve_election + check_edge_union_safety over the"
            " reference graph (degrading union-unsafe kinds to uniform"
            " record_index with a comment naming the gate), and aligns each"
            " dim's id column to its elected surface"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
