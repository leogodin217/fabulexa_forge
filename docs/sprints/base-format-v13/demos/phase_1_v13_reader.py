#!/usr/bin/env python
"""
Demo: the v13 reader — version gate, key-space texture, four-parameter union safety
Sprint: base-format-v13
Phase: 1

Builds a temp emit through the fixture authority with a partitioned
`presentation_keys` block carrying texture (`start`, one entry with `gaps`),
opens it (the gate accepts the supported version), prints the typed registry
view, and prints `union_safe` verdicts for three pairs. Exits non-zero on any
wrong verdict.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import duckdb

# The fixture authority lives in the test tree; make it importable standalone.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests"))

from _support.sidecar_builder import (  # noqa: E402
    enum_options,
    identity_column,
    key_space,
    partition_key,
    prop_column,
    write_emit,
)

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION  # noqa: E402
from fabulexa_forge.reader import KeySpace, open_emit, union_safe  # noqa: E402

_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
]

# Two record_index sub-types with incomparable prefixes: union-safe, so the
# kind rollup is a branch-scoped, stable whole-column claim.
_PRESENTATION_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {
            "bus": partition_key(
                key_space("record_index", prefix="BUS-", width=4, start=1)
            ),
            "driver": partition_key(
                key_space(
                    "record_index", prefix="DRV-", width=4, start=0, gaps=(0.25, 3)
                )
            ),
        },
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
    }
}


def _write_demo_emit(dest: Path) -> None:
    """Write a one-table temp emit carrying the texture-bearing registry."""
    conn = duckdb.connect(str(dest / "run.duckdb"))
    ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ACTOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({ddl})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a1", "DRV-0000", 1, True, 1, 0, "driver"],
    )
    conn.close()
    write_emit(
        dest,
        tables=[
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": _ACTOR_COLUMNS,
                "rows": 1,
            }
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 1}],
        extra={
            "enum_domains": {"actor": {"actor_type": enum_options("bus", "driver")}},
            "presentation_keys": _PRESENTATION_KEYS,
            "record_roles": {"actor": "dimension"},
        },
    )


def _verdict(label: str, a: KeySpace, b: KeySpace, expected: bool) -> bool:
    """Print one union_safe verdict; return whether it matches `expected`."""
    actual = union_safe(a, b)
    ok = actual == expected
    status = "ok" if ok else "WRONG"
    print(f"  {label}: union_safe={actual} (expected {expected}) {status}")
    return ok


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _write_demo_emit(emit_dir)
        with open_emit(emit_dir) as emit:
            print(
                f"open_emit accepted base_format_version "
                f"{emit.sidecar.base_format_version} "
                f"(supported: {SUPPORTED_BASE_FORMAT_VERSION})"
            )
            keys = emit.sidecar.presentation_keys()
            assert keys is not None
            print("Typed registry view:")
            for kind in keys.kinds():
                for sub_type in keys.sub_types(kind):
                    print(
                        f"  {kind}.{sub_type}: {keys.key_for(kind, sub_type).key_space}"
                    )
                print(f"  {kind} rollup: {keys.whole_table_claim(kind)}")
            bus = keys.key_for("actor", "bus").key_space
            driver = keys.key_for("actor", "driver").key_space

    print("Union-safety verdicts:")
    same_index = KeySpace("record_index", "ID-", 4, 1, None)
    shifted = KeySpace("record_index", "ID-", 4, 0, None)
    verdicts = [
        _verdict(
            "identical four-parameter record_index pair", same_index, same_index, True
        ),
        _verdict("same prefix/width, differing start", same_index, shifted, False),
        _verdict("incomparable-prefix digit pair (registry)", bus, driver, True),
    ]
    if not all(verdicts):
        print("FAILURE: a union_safe verdict was wrong")
        return 1
    print("SUCCESS: v13 emit opened; texture parsed; union_safe verdicts correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
